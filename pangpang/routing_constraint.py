"""Operator tool for durable Work routing restrictions.

python -m pangpang.routing_constraint --data DATA show
python -m pangpang.routing_constraint --data DATA clear --work WORK_ID

Previously, Service.send turned a chat message that merely mentioned a host or
installation id into `required_host_id` / `required_installation_id` on the Work
("不要再用 X" pinned X). Those fields record no provenance, so they are never
cleared automatically: an unknown-origin restriction stays fail-closed. This tool
lists them and removes one Work's restriction by exact id, with the service
stopped (single DB writer). Children inherit restrictions at creation; clear each
affected Work explicitly. A Work in a Routine lineage is refused: its restriction
is the approved standing-authority envelope and changes only by editing and
re-approving the Routine.
"""
import argparse
from pathlib import Path

FIELDS = ("required_host_id", "required_installation_id")
OPEN = ("active", "waiting")
MAX_DEPTH = 64


def lineage_kind(store, work):
    """'routine' if the Work or any ancestor is a Routine Work, else the root
    Work's kind; 'unresolved' if the parent chain cannot be followed. A Routine
    lineage's restriction is its approved standing-authority envelope, not
    a legacy mis-pin."""
    from .store import Fault
    current, seen = work, set()
    while True:
        if current.get("kind") == "routine" or current.get("routine_id"):
            return "routine"
        parent = current.get("parent_work_id")
        if not parent:
            return current.get("kind") or "unknown"
        if parent in seen or len(seen) >= MAX_DEPTH:
            return "unresolved"
        seen.add(parent)
        try:
            current = store.get("work", parent)
        except Fault:
            return "unresolved"


def constrained(store):
    out = []
    for status in OPEN:
        page = 0
        while True:
            rows = store.find("work", status=status, limit=1000, after=page)
            out += [w for w in rows if any(w.get(f) for f in FIELDS)]
            if len(rows) < 1000:
                break
            page += 1000
    return out


def clear(store, work_id):
    from .store import Fault
    work = store.get("work", work_id)
    lineage = lineage_kind(store, work)
    if lineage == "routine":
        # Widening an approved envelope requires changing the Routine authority
        # and re-approving its digest, never an offline edit of one fire's Work.
        raise Fault("routine_authority_reapproval_required", 409)
    if lineage == "unresolved":
        raise Fault("work_lineage_unresolved", 409)
    removed = {f: work.pop(f) for f in FIELDS if f in work}
    if removed:
        store.put("work", work, expected=work["revision"])
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show / clear durable Work routing restrictions")
    parser.add_argument("action", choices=["show", "clear"])
    parser.add_argument("--data", required=True, help="coordinator data directory (service stopped)")
    parser.add_argument("--work", help="exact Work id to clear")
    args = parser.parse_args(argv)
    if args.action == "clear" and not args.work:
        parser.error("clear requires --work ID")
    from .consent import _open
    lock, store = _open(Path(args.data).resolve())
    try:
        if args.action == "show":
            rows = constrained(store)
            for w in rows:
                print("%s\t%s\tlineage=%s\thost=%s\tinstallation=%s" % (
                    w["id"], w.get("kind"), lineage_kind(store, w),
                    w.get("required_host_id") or "-", w.get("required_installation_id") or "-"))
            print("共 %d 个未结束的 Work 带路由限制。" % len(rows))
            return 0
        from .store import Fault
        try:
            removed = clear(store, args.work)
        except Fault as error:
            print("拒绝清除 %s：%s（Routine 的限制请修改 Routine 授权并重新批准）" % (args.work, error.code))
            return 2
        print("已清除 %s：%s" % (args.work, removed) if removed else "%s 没有路由限制，未改变。" % args.work)
        return 0
    finally:
        store.close()
        lock.file.close()


if __name__ == "__main__":
    raise SystemExit(main())
