"""The LLM usage ledger — token accounting + per-call I/O, kept apart from the graph.

Moko records every brain-model call here: token counts (labelled by activity `kind`) plus
the exact system prompt / request / reply. It shares the Store's SQLite connection but has
no coupling to the subject/relationship graph — the monitor's cost view and the dream's
pruning read it, the graph never does. Lives behind `store.usage`.
"""

from __future__ import annotations

import aiosqlite

from ..clock import utc_isoformat
from .models import new_id


class UsageLedger:
    """Token accounting + per-call I/O for the brain model. Shares the Store's connection."""

    # Columns for the recent-calls LIST — deliberately NOT the big I/O blobs (those load only
    # in the per-call detail, so the list + its polling stay light).
    _LIST_COLS = ("id, created_at, kind, model, input_tokens, output_tokens,"
                  " cache_read_tokens, cache_write_tokens")

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def add_llm_call(
        self,
        *,
        kind: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        system_prompt: str | None = None,
        request: str | None = None,
        response: str | None = None,
        tool_calls: str | None = None,
    ) -> str:
        """Record one brain-model call: its token usage (labelled by activity `kind`) and the
        exact I/O — the system prompt, the request messages, the reply text, and the tool
        calls the model made (JSON). Returns the row id so the caller can announce it on the
        feed / deep-link to the detail."""
        rid = new_id()
        await self.conn.execute(
            "INSERT INTO llm_calls (id, created_at, kind, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, system_prompt, request, response, tool_calls)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, utc_isoformat(), kind, model, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, system_prompt, request, response, tool_calls),
        )
        await self.conn.commit()
        return rid

    async def recent_llm_calls(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        """The most recent LLM calls, newest-first — summary rows (no I/O blobs) for the list."""
        cur = await self.conn.execute(
            f"SELECT {self._LIST_COLS} FROM llm_calls"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def prune_llm_call_io(self, before: str) -> int:
        """Drop the stored I/O blobs (system_prompt/request/response) of llm_calls rows older
        than `before` (ISO UTC), keeping the lightweight token counts forever. Bounds disk
        growth (the system prompt is re-stored every call) while preserving the cost ledger.
        Returns how many rows were cleared."""
        cur = await self.conn.execute(
            "UPDATE llm_calls SET system_prompt = NULL, request = NULL, response = NULL,"
            " tool_calls = NULL"
            " WHERE created_at < ? AND (system_prompt IS NOT NULL OR request IS NOT NULL"
            " OR response IS NOT NULL OR tool_calls IS NOT NULL)",
            (before,),
        )
        await self.conn.commit()
        return cur.rowcount

    async def llm_call_detail(self, call_id: str) -> dict | None:
        """One call's full record — token counts AND the exact system prompt / request / reply —
        for the monitor's per-call I/O view."""
        cur = await self.conn.execute("SELECT * FROM llm_calls WHERE id = ?", (call_id,))
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def llm_call_summary(self, *, since: str | None = None) -> dict:
        """Token totals + a per-activity breakdown, optionally only since `since` (ISO UTC) —
        so the tab can show all-time and rolling windows. Calls, input, output, and both cache
        figures are summed per `kind`, biggest spender first."""
        where = "WHERE created_at >= ?" if since else ""
        params = (since,) if since else ()
        cur = await self.conn.execute(
            "SELECT kind, COUNT(*) AS calls,"
            " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,"
            " COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens"
            f" FROM llm_calls {where} GROUP BY kind"
            " ORDER BY input_tokens + output_tokens DESC",
            params,
        )
        by_kind = [dict(r) for r in await cur.fetchall()]
        fields = ("calls", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens")
        totals = {f: sum(r[f] for r in by_kind) for f in fields}
        return {"by_kind": by_kind, "totals": totals}
