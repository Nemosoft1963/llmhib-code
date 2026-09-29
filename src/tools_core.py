"""コード作成エージェントが使うツールの共通実装(バックエンド非依存)。

Claude(Anthropic Tool Runner)とローカルLLM(Ollama)の両方から、
ここにある同じロジックを呼び出す。すべてのファイル操作・コマンド実行は
「プロジェクト」(PROJECTS_ROOT配下のサブディレクトリ)に閉じ込める。
モデルが返す path / command / project は信用しない前提で、必ず検証を行う。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/app/workspace")).resolve()
PROJECTS_ROOT = WORKSPACE_ROOT / "projects"
# 注意: ここで PROJECTS_ROOT を作成しない。main.py の起動時マイグレーションが
# 「PROJECTS_ROOT が存在するか」を「旧レイアウトからの移行が必要か」の判定に使うため、
# 実際に必要になるまで(Workspace初回作成 / list_projects呼び出し時)遅延作成する。

DEFAULT_PROJECT = "default"
_PROJECT_NAME_MAX_LEN = 100
# パス区切り・制御文字・OS上使えない記号(Windowsエクスポート時も考慮)のみ禁止する。
# 日本語などUnicode文字は許可する(URLはJS側でencodeURIComponentするため問題ない)。
_INVALID_PROJECT_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# run_command で許可する実行ファイルのアローリスト(ブロックリストではない)
ALLOWED_COMMANDS = {
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "node",
    "npm",
    "npx",
    "git",
    "ls",
    "mkdir",
}

# シェル演算子(これらを含むコマンドは拒否する)
FORBIDDEN_OPERATORS = ("&&", "||", "|", ";", "`", "$(")

COMMAND_TIMEOUT_SEC = 60


class WorkspacePathError(Exception):
    """プロジェクトディレクトリ外へのアクセスが検出された場合の例外。"""


class InvalidProjectNameError(ValueError):
    """プロジェクト名が不正な場合の例外(400として扱えるようValueErrorを継承)。"""


def is_valid_project_name(name: str) -> bool:
    """プロジェクト名の妥当性チェック。

    日本語などUnicode文字・空白を含む名前は許可する。禁止するのは: 空/長すぎる、
    前後の空白、"."/".."そのものや末尾の"."、パス区切り文字やOS上使えない記号・制御文字、
    Windowsの予約デバイス名(将来Windows側へエクスポートされる可能性を考慮)。
    """
    if not name or len(name) > _PROJECT_NAME_MAX_LEN:
        return False
    if name != name.strip():
        return False
    if name in (".", "..") or name.endswith("."):
        return False
    if _INVALID_PROJECT_CHARS_RE.search(name):
        return False
    if name.upper() in _RESERVED_WINDOWS_NAMES:
        return False
    return True


def _meta_path(project: str) -> Path:
    """プロジェクトのメタデータ(エクスポート先など)を保存するファイル。

    プロジェクト自身のディレクトリ(git管理・/filesの一覧対象)の外、
    PROJECTS_ROOT直下に隠しファイルとして置く(プロジェクトの中身を汚さないため)。
    """
    return PROJECTS_ROOT / f".{project}.meta.json"


def get_project_meta(project: str) -> dict[str, Any]:
    """プロジェクトのメタデータを読む(無ければ空dict)。"""
    path = _meta_path(project)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def set_project_meta(project: str, **updates: Any) -> dict[str, Any]:
    """プロジェクトのメタデータをマージ更新して保存する。"""
    if not is_valid_project_name(project):
        raise InvalidProjectNameError(f"不正なプロジェクト名です: {project!r}")
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    meta = get_project_meta(project)
    meta.update(updates)
    _meta_path(project).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def list_projects() -> list[str]:
    """既存プロジェクト名の一覧を返す(無ければ`default`のみを作って返す)。"""
    if not PROJECTS_ROOT.exists():
        get_workspace(DEFAULT_PROJECT)  # PROJECTS_ROOT と default プロジェクトを作成する
        return [DEFAULT_PROJECT]
    names = sorted(p.name for p in PROJECTS_ROOT.iterdir() if p.is_dir())
    if not names:
        get_workspace(DEFAULT_PROJECT)
        return [DEFAULT_PROJECT]
    return names


class Workspace:
    """1プロジェクト分のファイル操作をカプセル化する。"""

    def __init__(self, project: str):
        if not is_valid_project_name(project):
            raise InvalidProjectNameError(
                f"不正なプロジェクト名です: {project!r} "
                '(先頭・末尾の空白、"."/".."、/ \\ < > : " | ? * などの記号、'
                f"制御文字は使用できません。{_PROJECT_NAME_MAX_LEN}文字以内)"
            )
        self.project = project
        self.root = (PROJECTS_ROOT / project).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        """モデルから渡された相対パスを、このプロジェクトのroot配下に限定して解決する。

        シンボリックリンクや `..` によるパストラバーサルを防ぐため、
        resolve() 後に is_relative_to() で範囲チェックを行う。
        """
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspacePathError(
                f"プロジェクト外のパスへのアクセスは禁止されています: {relative_path}"
            )
        return candidate

    def list_files_recursive(self) -> list[str]:
        """プロジェクト配下の全ファイルを、rootからの相対パス(スラッシュ区切り)で列挙する。

        チェックポイント機能が使う `.git/` の中身は除外する(UI/モデルへのノイズになるため)。
        """
        return sorted(
            str(p.relative_to(self.root)).replace(os.sep, "/")
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.root).parts
        )

    def read_file(self, path: str) -> str:
        """プロジェクト内のファイルをテキストとして読み込む。"""
        try:
            target = self._resolve(path)
        except WorkspacePathError as e:
            return f"エラー: {e}"

        if not target.is_file():
            return f"エラー: ファイルが見つかりません: {path}"
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"エラー: テキストとして読み込めないファイルです(バイナリの可能性): {path}"

    def write_file(self, path: str, content: str) -> str:
        """プロジェクト内にファイルを新規作成、または上書きする。親ディレクトリが無ければ自動作成。"""
        try:
            target = self._resolve(path)
        except WorkspacePathError as e:
            return f"エラー: {e}"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"書き込み完了: {path} ({len(content)} 文字)"

    def edit_file(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        """既存ファイルの一部だけを置換する(全体は書き換えない)。

        - old_string がファイル内に無い、または複数あって replace_all でない場合はエラー(誤爆防止)。
        - 元ファイルの改行コード(CRLF/LF)とそれ以外のバイトは変えない。
          old_string / new_string が LF だけで、ファイルが CRLF の場合は CRLF に読み替えて照合する。
        """
        try:
            target = self._resolve(path)
        except WorkspacePathError as e:
            return f"エラー: {e}"
        if not target.is_file():
            return f"エラー: ファイルが見つかりません: {path}"
        if not old_string:
            return "エラー: old_string が空です。置換する文字列を指定してください。"
        if old_string == new_string:
            return "エラー: old_string と new_string が同じです。"
        try:
            text = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return f"エラー: UTF-8 として読めないファイルは編集できません: {path}"

        candidates = [(old_string, new_string)]
        cr, lf = chr(13), chr(10)
        if cr not in old_string and lf in old_string:
            candidates.append(
                (
                    old_string.replace(lf, cr + lf),
                    new_string if cr in new_string else new_string.replace(lf, cr + lf),
                )
            )
        for old, new in candidates:
            count = text.count(old)
            if count == 0:
                continue
            if count > 1 and not replace_all:
                return (
                    f"エラー: old_string が {count} 箇所に一致しました。前後の文脈を足して一意にするか、"
                    "replace_all=true を指定してください。"
                )
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            target.write_bytes(updated.encode("utf-8"))
            return f"編集完了: {path} ({count if replace_all else 1} 箇所を置換)"
        return (
            f"エラー: old_string が {path} 内に見つかりません。read_file で現在の内容を確認し、"
            "空白・改行・インデントまで正確に一致させてください。"
        )

    def list_directory(self, path: str = ".") -> str:
        """プロジェクト内のディレクトリ内容を一覧表示する。"""
        try:
            target = self._resolve(path)
        except WorkspacePathError as e:
            return f"エラー: {e}"

        if not target.is_dir():
            return f"エラー: ディレクトリが見つかりません: {path}"

        entries = sorted(
            (p for p in target.iterdir() if p.name != ".git"), key=lambda p: p.name
        )
        if not entries:
            return "(空のディレクトリ)"
        return "\n".join(f"{p.name}/" if p.is_dir() else p.name for p in entries)

    def run_command(self, command: str) -> str:
        """プロジェクト内で、許可されたコマンドのみを実行する。

        許可コマンド: python, python3, pip, pip3, pytest, node, npm, npx, git, ls, mkdir。
        シェル演算子(&&, ||, |, ;, バッククォート, $())は使用できない。タイムアウトは60秒。
        """
        if any(op in command for op in FORBIDDEN_OPERATORS):
            return "エラー: シェル演算子(&&, |, ; など)は使用できません。1コマンドずつ実行してください。"

        try:
            parts = shlex.split(command)
        except ValueError as e:
            return f"エラー: コマンドの解析に失敗しました: {e}"

        if not parts:
            return "エラー: 空のコマンドです"

        if parts[0] not in ALLOWED_COMMANDS:
            allowed = ", ".join(sorted(ALLOWED_COMMANDS))
            return f"エラー: 許可されていないコマンドです: {parts[0]} (許可されているコマンド: {allowed})"

        try:
            result = subprocess.run(
                parts,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return f"エラー: コマンドがタイムアウトしました({COMMAND_TIMEOUT_SEC}秒): {command}"
        except FileNotFoundError:
            return f"エラー: 実行ファイルが見つかりません: {parts[0]}"

        output = [f"$ {command}", f"(終了コード: {result.returncode})"]
        if result.stdout:
            output.append(f"--- stdout ---\n{result.stdout}")
        if result.stderr:
            output.append(f"--- stderr ---\n{result.stderr}")
        return "\n".join(output)


_workspaces: dict[str, Workspace] = {}


def get_workspace(project: str = DEFAULT_PROJECT) -> Workspace:
    """プロジェクト名からWorkspaceを取得する(初回アクセス時にディレクトリを作成)。"""
    ws = _workspaces.get(project)
    if ws is None:
        ws = Workspace(project)
        _workspaces[project] = ws
    return ws
