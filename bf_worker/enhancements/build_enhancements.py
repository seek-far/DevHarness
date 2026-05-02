def build_enhancements(specs: list[dict]) -> list[tuple]:
    """Translate `enhancements` config entries into (hook_name, callback) tuples.

    A spec entry looks like: {"kind": "memory", "store_path": "...", "top_k": 2}.
    The factory dispatches on `kind`; add a branch here when introducing a new
    enhancement.
    """
    out: list[tuple] = []
    for spec in specs:
        kind = spec.get("kind")
        if kind == "memory":
            from enhancements.memory import build_memory_callbacks
            out.extend(build_memory_callbacks(
                store_path=spec.get("store_path", "evaluation/memory/store.json"),
                top_k=int(spec.get("top_k", 2)),
                write_back=bool(spec.get("write_back", True)),
            ))
        else:
            raise ValueError(f"unknown enhancement kind: {kind!r}")
    return out