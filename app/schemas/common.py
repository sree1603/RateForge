from typing import Annotated

from pydantic import Field

# Control characters (C0 range and DEL) are never legitimate in a name. NUL in particular is
# rejected by PostgreSQL, so letting it through turns a bad request into a 500.
NO_CONTROL_CHARACTERS = r"^[^\x00-\x1f\x7f]+$"

Name = Annotated[str, Field(min_length=1, max_length=100, pattern=NO_CONTROL_CHARACTERS)]
