from collections.abc import Awaitable, Callable, Hashable, Sequence
from typing import Self

START: str
END: str

class Edge:
    source: str
    target: str
    conditional: bool

class DrawableGraph:
    @property
    def edges(self) -> list[Edge]: ...

class CompiledStateGraph[StateT]:
    async def ainvoke(self, input: StateT) -> StateT: ...
    def get_graph(self) -> DrawableGraph: ...

class StateGraph[StateT]:
    def __init__(self, state_schema: type[StateT]) -> None: ...
    def add_node(
        self,
        node: str,
        action: Callable[[StateT], object | Awaitable[object]],
    ) -> Self: ...
    def add_edge(self, start_key: str | list[str], end_key: str) -> Self: ...
    def add_conditional_edges(
        self,
        source: str,
        path: Callable[
            [StateT],
            Hashable | Sequence[Hashable] | Awaitable[Hashable | Sequence[Hashable]],
        ],
        path_map: dict[Hashable, str] | list[str] | None = None,
    ) -> Self: ...
    def compile(self, *, name: str | None = None) -> CompiledStateGraph[StateT]: ...
