from .schema import ActiveContext, ContextItem, ItemStatus, ItemType


class ContextGraph:
    """Small deterministic state layer; durable writes live in MemoryStore."""

    def __init__(self, context: ActiveContext):
        self.context = context

    def add(self, item: ContextItem) -> None:
        if item.supersedes:
            previous = next((old for old in self.context.items if old.id == item.supersedes), None)
            if previous is None:
                raise ValueError(f"superseded context item does not exist: {item.supersedes}")
            if previous.status == ItemStatus.SUPERSEDED:
                raise ValueError(f"context item is already superseded: {item.supersedes}")
            for old in self.context.items:
                if old.id == item.supersedes:
                    old.status = ItemStatus.SUPERSEDED
        if any(old.id == item.id for old in self.context.items):
            raise ValueError(f"duplicate context item id: {item.id}")
        self.context.items.append(item)
        self.context.context_version += 1

    def active(self, item_type: ItemType | None = None) -> list[ContextItem]:
        return [
            x for x in self.context.items
            if x.status == ItemStatus.ACTIVE
            and (item_type is None or x.type == item_type)
        ]
