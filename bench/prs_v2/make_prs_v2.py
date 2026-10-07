#!/usr/bin/env python3
"""spec.yaml から PR の課題 v2 を作る.

    python bench/prs_v2/make_prs_v2.py ../dev-environment           # tasks/T??.diff・cases.yaml・answers.yaml を書く
    python bench/prs_v2/make_prs_v2.py ../dev-environment --check   # 作り直した結果がコミット済みと同じか（書き込まない）

- dev-environment は読むだけ（`git archive` で base_commit を一時フォルダに取り出す. worktree も作らない）
- ケースごとに、本体の編集（edits）と、関係のない小さな変更（noise）を当てて `git diff` を保存する
- ID は種類が分からないよう、spec.yaml の seed で並べ替えて T01〜 を振る
- 答え（answers.yaml）には、変更後のファイルでの行（anchor の位置）を入れる
- 最後に leak_check.py で、答えが差分・題名・説明・プロンプトに漏れていないかを確かめる（漏れていれば失敗）
"""

from __future__ import annotations

import argparse
import io
import random
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "prs"))

import leak_check  # noqa: E402
from make_prs import apply_edit  # noqa: E402  v1 と同じ編集の書き方

KINDS = ("bug", "clean", "decoy")
MAX_TOTAL_BYTES = 2_000_000


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def export_base(repo: Path, commit: str, dest: Path) -> None:
    """base_commit のファイル一式を dest に取り出し、比べるための git リポジトリにする（元のリポジトリには書き込まない）."""
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], check=True, capture_output=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "commit", "-q", "-m", "base")


def validate(spec: dict) -> None:
    keys = [c["key"] for c in spec["cases"]]
    if len(keys) != len(set(keys)):
        raise SystemExit("spec.yaml: key が重複しています")
    for c in spec["cases"]:
        if c["kind"] not in KINDS:
            raise SystemExit(f"{c['key']}: kind は {KINDS} のどれか")
        for n in c.get("noise", []):
            if n not in spec["noise"]:
                raise SystemExit(f"{c['key']}: noise {n} がありません")
        if c["kind"] == "bug":
            if not c.get("noise"):
                raise SystemExit(f"{c['key']}: バグの PR には関係のない変更（noise）を混ぜる")
            a = c["answer"]
            for k in ("file", "anchor", "severity", "severity_ok", "summary", "detail", "leak_terms"):
                if not a.get(k):
                    raise SystemExit(f"{c['key']}: answer.{k} がありません")
            if a["severity"] not in a["severity_ok"]:
                raise SystemExit(f"{c['key']}: severity は severity_ok に含めること")
        if c["kind"] == "decoy" and not c["answer"].get("why_correct"):
            raise SystemExit(f"{c['key']}: おとりには answer.why_correct が要ります")


def assign_ids(spec: dict) -> dict[str, str]:
    keys = [c["key"] for c in spec["cases"]]
    order = keys[:]
    random.Random(spec["seed"]).shuffle(order)
    width = max(2, len(str(len(order))))
    return {k: f"T{i:0{width}d}" for i, k in enumerate(order, start=1)}


def locate(root: Path, file: str, anchor: str, key: str) -> list[int]:
    text = (root / file).read_text(encoding="utf-8")
    n = text.count(anchor)
    if n != 1:
        raise SystemExit(f"{key}: anchor が変更後の {file} で {n} か所に一致しました（1 か所であること）: {anchor!r}")
    start = text[: text.index(anchor)].count("\n") + 1
    return [start, start + anchor.count("\n")]


def build(repo: Path, spec: dict) -> tuple[dict[str, str], dict, dict]:
    """（ID → 差分, 公開する cases, 答え）を作る."""
    validate(spec)
    ids = assign_ids(spec)
    diffs: dict[str, str] = {}
    public, answers = [], []
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "base"
        export_base(repo, spec["base_commit"], wt)
        for c in spec["cases"]:
            cid = ids[c["key"]]
            for e in c.get("edits", []):
                apply_edit(wt, e, c["key"])
            for n in c.get("noise", []):
                for e in spec["noise"][n]:
                    apply_edit(wt, e, f"{c['key']}（noise {n}）")
            diff = git(wt, "diff", "--no-color", "--no-ext-diff", "HEAD")
            if not diff.strip():
                raise SystemExit(f"{c['key']}: 差分がありません")
            diffs[cid] = diff
            public.append({"id": cid, "title": c["title"], "description": c["description"]})
            a = {"id": cid, "key": c["key"], "kind": c["kind"]}
            ans = c.get("answer") or {}
            if c["kind"] == "bug":
                a["file"] = ans["file"]
                a["lines"] = locate(wt, ans["file"], ans["anchor"], c["key"])
                a.update({k: ans[k] for k in ("symbol", "severity", "severity_ok", "summary", "detail", "leak_terms") if k in ans})
            elif c["kind"] == "decoy":
                a["file"] = ans["file"]
                a.update({k: ans[k] for k in ("why_correct", "looks_risky") if k in ans})
            a["changed_files"] = sorted({p for p, _, _ in leak_check.changed_lines(diff)})
            a["noise"] = list(c.get("noise", []))
            answers.append(a)
            git(wt, "reset", "-q", "--hard", "HEAD")
            git(wt, "clean", "-qfd")
    public.sort(key=lambda x: x["id"])
    answers.sort(key=lambda x: x["id"])
    cases_doc = {
        "target": spec["target"],
        "base_commit": spec["base_commit"],
        "diff_dir": "tasks",
        "cases": public,
    }
    answers_doc = {"base_commit": spec["base_commit"], "answers": answers}
    return diffs, cases_doc, answers_doc


HEADER_CASES = (
    "# PR の課題 v2（モデルに渡す題名と説明）. make_prs_v2.py が spec.yaml から作る. 手で書き換えない\n"
    "# 種類（バグ・問題なし・おとり）はここには書かない（answers.yaml にだけある）\n"
)
HEADER_ANSWERS = (
    "# PR の課題 v2 の答え（採点する人だけが見る. モデルにもプロンプトにも渡さない）. make_prs_v2.py が spec.yaml から作る\n"
    "# lines は変更後のファイルでの行（anchor の位置）. kind: bug（不具合を 1 つ）/ clean（問題なし）/ decoy（危なそうだが正しい）\n"
)


def render(diffs: dict[str, str], cases_doc: dict, answers_doc: dict) -> dict[str, str]:
    """書き出すファイル（HERE からの相対パス → 中身）."""
    files = {f"tasks/{cid}.diff": d for cid, d in sorted(diffs.items())}
    files["cases.yaml"] = HEADER_CASES + yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False, width=1000)
    files["answers.yaml"] = HEADER_ANSWERS + yaml.safe_dump(answers_doc, allow_unicode=True, sort_keys=False, width=1000)
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path, help="dev-environment のパス（読むだけ）")
    ap.add_argument("--check", action="store_true", help="書き込まず、コミット済みのファイルと同じかだけを確かめる")
    args = ap.parse_args(argv)
    spec = yaml.safe_load((HERE / "spec.yaml").read_text(encoding="utf-8"))
    diffs, cases_doc, answers_doc = build(args.repo.resolve(), spec)
    files = render(diffs, cases_doc, answers_doc)
    total = sum(len(v.encode("utf-8")) for v in files.values())
    if total > MAX_TOTAL_BYTES:
        raise SystemExit(f"課題の合計が {total:,} バイトで、上限 {MAX_TOTAL_BYTES:,} を超えています")
    if args.check:
        stale = [p for p, v in files.items() if not (HERE / p).exists() or (HERE / p).read_text(encoding="utf-8") != v]
        extra = sorted(str(p.relative_to(HERE)) for p in (HERE / "tasks").glob("*.diff") if str(p.relative_to(HERE)) not in files)
        for p in stale + extra:
            print(f"作り直すと変わります: {p}", file=sys.stderr)
        return 1 if stale or extra else 0
    (HERE / "tasks").mkdir(exist_ok=True)
    for old in (HERE / "tasks").glob("*.diff"):
        old.unlink()
    for p, v in files.items():
        (HERE / p).write_text(v, encoding="utf-8")
    kinds = {k: sum(1 for a in answers_doc["answers"] if a["kind"] == k) for k in KINDS}
    print(f"{len(diffs)} 件（bug {kinds['bug']}・clean {kinds['clean']}・decoy {kinds['decoy']}）、合計 {total:,} バイト")
    errs = leak_check.check_all(HERE)
    for e in errs:
        print(f"漏れ: {e}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
