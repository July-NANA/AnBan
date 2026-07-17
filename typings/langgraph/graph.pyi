from collections.abc import Awaitable, Callable
from typing import Self

START: str
END: str

class Edge:
    source: str
    target: str

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
        action: Callable[[StateT], Awaitable[object]],
    ) -> Self: ...
    def add_edge(self, start_key: str, end_key: str) -> Self: ...
    def compile(self, *, name: str | None = None) -> CompiledStateGraph[StateT]: ...
