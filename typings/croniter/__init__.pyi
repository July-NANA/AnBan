from datetime import datetime

class CroniterError(Exception): ...

class croniter:
    def __init__(
        self,
        expr_format: str,
        start_time: datetime,
        ret_type: type[float] = float,
        day_or: bool = True,
        max_years_between_matches: int | None = None,
    ) -> None: ...
    @classmethod
    def is_valid(cls, expression: str, *, strict: bool = False) -> bool: ...
    def get_next(self, ret_type: type[datetime]) -> datetime: ...
