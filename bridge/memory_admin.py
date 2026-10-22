"""本地记忆 CLI（纯标准库）。

用法（在 bridge 目录）：
    python memory_admin.py list [--kind todo] [--json]
    python memory_admin.py pin <id>
    python memory_admin.py unpin <id>
    python memory_admin.py delete <id> [--purge]
    python memory_admin.py export [--session dev1] [--out file.json]
    python memory_admin.py stats
    python memory_admin.py why <id>

与 bridge 共用 state.lock；写操作拿不到锁时直接报错退出。
"""

import argparse
import json
import os
import sys

import memory_maintenance
import memory_store


def collect_list(store, kind=None):
    facts = [item for item in store.active_facts()
             if kind is None or item.get("kind") == kind]
    episodes = [item for item in store.active_episodes()
                if kind is None or item.get("kind") == kind]
    return {"facts": facts, "episodes": episodes}


def collect_stats(store):
    facts = store.facts()
    episodes = store.episodes()
    state = store.state
    active_facts = [item for item in facts if item.get("status") == "active"]
    active_episodes = [item for item in episodes if item.get("status") == "active"]
    pinned = [item for item in active_facts if item.get("pinned")]
    pending = [item for item in active_episodes
               if item.get("kind") == "preference"
               and int(item.get("hits") or 0) >= memory_maintenance.PROMOTE_HITS]
    return {
        "facts_active": len(active_facts),
        "facts_tombstone": len(facts) - len(active_facts),
        "episodes_active": len(active_episodes),
        "episodes_tombstone": len(episodes) - len(active_episodes),
        "pinned": len(pinned),
        "pinned_overflow": len(pinned) > 2,
        "pending_promotion": len(pending),
        "at_capacity": bool(state.get("at_capacity")),
        "last_maintenance": state.get("last_maintenance"),
        "turns_since_maintenance": int(
            state.get("turns_since_maintenance") or 0),
        "schema_version": state.get("schema_version"),
        "directory_size": store.directory_size(),
        "counters": store.counters(),
        "read_only": bool(store.read_only),
    }


def export_data(store, session_id=None):
    return store.export_active(session_id=session_id)


def explain(store, record_id):
    record = store.find(record_id)
    if record is None:
        return None
    return {
        "record": record,
        "history": list(record.get("history") or []),
        "tombstone_reason": record.get("tombstone_reason"),
        "pinned": bool(record.get("pinned")),
        "hits": int(record.get("hits") or 0),
        "last_used_at": record.get("last_used_at"),
    }


def delete_record(store, record_id, purge=False, now=None):
    if purge:
        removed = store.purge(record_id) > 0
        if removed:
            store.clear_sticky_for_todo(record_id, now=now)
        return removed
    fact = store.find_fact(record_id)
    if fact is not None:
        return store.delete_fact(record_id, now=now)
    episode = store.find_episode(record_id)
    if episode is not None:
        deleted = store.delete_episode(record_id, now=now)
        store.clear_sticky_for_todo(record_id, now=now)
        return deleted
    return False


def _print_json(value, out=None):
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    (out or sys.stdout).write(text + "\n")


def _print_records(data, out=None):
    out = out or sys.stdout
    for label, items in (("fact", data["facts"]),
                         ("episode", data["episodes"])):
        for item in items:
            flags = []
            if item.get("pinned"):
                flags.append("pinned")
            if item.get("status") != "active":
                flags.append(item.get("status"))
            suffix = (" [" + ",".join(flags) + "]") if flags else ""
            out.write("%s\t%s\t%s%s\t%s\n" % (
                item.get("id"), label, item.get("kind"),
                suffix, item.get("text")))


def cmd_list(args):
    store = memory_store.MemoryStore(args.dir)
    data = collect_list(store, kind=args.kind)
    if args.json:
        _print_json(data)
    else:
        _print_records(data)
    return 0


def cmd_pin(args):
    store = memory_store.MemoryStore(args.dir)
    if not store.set_pinned(args.id, True):
        print("not found: %s" % args.id, file=sys.stderr)
        return 1
    print("pinned: %s" % args.id)
    return 0


def cmd_unpin(args):
    store = memory_store.MemoryStore(args.dir)
    if not store.set_pinned(args.id, False):
        print("not found: %s" % args.id, file=sys.stderr)
        return 1
    print("unpinned: %s" % args.id)
    return 0


def cmd_delete(args):
    store = memory_store.MemoryStore(args.dir)
    if not delete_record(store, args.id, purge=args.purge):
        print("not found: %s" % args.id, file=sys.stderr)
        return 1
    print(("purged: " if args.purge else "deleted: ") + args.id)
    return 0


def cmd_export(args):
    store = memory_store.MemoryStore(args.dir)
    data = export_data(store, session_id=args.session)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("exported: %s" % args.out)
    else:
        sys.stdout.write(text)
    return 0


def cmd_stats(args):
    store = memory_store.MemoryStore(args.dir)
    _print_json(collect_stats(store))
    return 0


def cmd_why(args):
    store = memory_store.MemoryStore(args.dir)
    data = explain(store, args.id)
    if data is None:
        print("not found: %s" % args.id, file=sys.stderr)
        return 1
    _print_json(data)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="voice-ai memory admin")
    parser.add_argument("--dir", default=None, help="memory directory")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list active records")
    list_parser.add_argument("--kind")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    pin = sub.add_parser("pin", help="pin a fact")
    pin.add_argument("id")
    pin.set_defaults(func=cmd_pin)

    unpin = sub.add_parser("unpin", help="unpin a fact")
    unpin.add_argument("id")
    unpin.set_defaults(func=cmd_unpin)

    delete = sub.add_parser("delete", help="tombstone or purge a record")
    delete.add_argument("id")
    delete.add_argument("--purge", action="store_true")
    delete.set_defaults(func=cmd_delete)

    export = sub.add_parser("export", help="export active content")
    export.add_argument("--session")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)

    stats = sub.add_parser("stats", help="show counts and state")
    stats.set_defaults(func=cmd_stats)

    why = sub.add_parser("why", help="show one record and history")
    why.add_argument("id")
    why.set_defaults(func=cmd_why)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (memory_store.MemoryBusy, memory_store.MemoryReadOnly) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
