from __future__ import annotations


def install_counting_loaders(monkeypatch, core_store):
    calls: list[str] = []
    mapping = {
        "reload_chat_hot_strict": "chat",
        "_load_frames_meta": "frames",
        "_load_world_books": "world_books",
        "_load_tokens": "tokens",
        "_load_push_state": "push_state",
        "_load_live_activity_state": "live_activity",
    }
    for method_name, label in mapping.items():
        monkeypatch.setattr(
            core_store.UserStore,
            method_name,
            lambda _self, value=label: calls.append(value),
        )
    return calls
