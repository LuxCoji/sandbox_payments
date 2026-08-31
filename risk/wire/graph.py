"""The account graph, maintained one transfer at a time.

The offline wire rail builds a graph over a finished dataset with pandas and
networkx, then counts patterns across the whole thing. A live simulation cannot
do that: the transfer has to be scored before the next one arrives, and the
graph is never finished.

So this maintains the same structure incrementally, inside a sliding window.
Everything outside the window is dropped, which keeps both memory and the cost
of a query bounded no matter how long the simulation runs.

## Why a window at all

Money laundering patterns are defined by *timing*, not just by shape. Ordinary
business forms loops constantly - a supplier pays a distributor who pays a
retailer who buys from the supplier. What distinguishes laundering is that the
loop closes fast, because the point is to move money, not to trade.

An earlier version of the offline rail used a thirty-day window on an
eighteen-day dataset. Every cycle trivially passed, 372,952 of them survived,
and 71.6% of all accounts were flagged. The window is the filter; a loose one
does nothing.

## Counts, not flags

"This account is on a cycle" is nearly useless. "This account is on three cycles
that each close within a day, and forwards 96% of what it receives" is a
different statement. Everything here is a count or a ratio.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_HOUR = 3_600 * NANOS_PER_SECOND
NANOS_PER_DAY = 24 * NANOS_PER_HOUR

# From the IBM paper the offline rail follows: one day for cycles, six hours for
# scatter-gather. The graph itself is kept for a week so that longer-running
# structures are still visible to the degree and pass-through features.
CYCLE_WINDOW_NS = 1 * NANOS_PER_DAY
SCATTER_GATHER_WINDOW_NS = 6 * NANOS_PER_HOUR
GRAPH_WINDOW_NS = 7 * NANOS_PER_DAY

# A laundering chain longer than this is not something a bounded search will
# find reliably, and the offline rail measured no gain past six.
MAX_CYCLE_LENGTH = 6

# Cycle search is the one expensive query here. Without a cap, a hub account
# with thousands of counterparties makes every transfer through it quadratic.
MAX_CYCLE_SEARCH_NODES = 400


@dataclass
class Edge:
    """Money moved from one account to another, at a time."""

    source: str
    destination: str
    amount_paise: int
    at_ns: float


@dataclass
class AccountStructure:
    """What the graph knows about one account, as of now."""

    out_degree: int = 0            # distinct accounts paid
    in_degree: int = 0             # distinct accounts paid by
    sent_total: int = 0
    received_total: int = 0
    sent_count: int = 0
    received_count: int = 0
    cycle_count: int = 0           # tight cycles this account sits on
    shortest_cycle: int = 0        # 0 when there is none
    fastest_cycle_hours: float = 0.0
    fan_out_burst: int = 0         # distinct payees inside the scatter window
    fan_in_burst: int = 0          # distinct payers inside the scatter window

    @property
    def passthrough(self) -> float:
        """Share of what came in that went straight back out.

        A mule account's defining property: money arrives and leaves, and very
        little stays. Returns 0.0 for an account that has received nothing,
        because a ratio with no denominator is not a high one - it is unknown,
        and treating unknown as suspicious would flag every account on its
        first outbound transfer.

        **Unbounded on purpose, and the caller must treat it as such.** A value
        above 1.0 means the account sent more than arrived by transfer, so the
        rest came from somewhere outside this graph - a salary, a deposit, a
        card refund. That is an ordinary person spending their own money, not a
        mule, and it is the opposite of the pattern. Measured on legitimate
        simulator traffic this ratio reached 2.46, and a rule reading "at least
        0.90" flagged 80% of honest accounts.
        """
        if self.received_total <= 0:
            return 0.0
        return self.sent_total / self.received_total


class TransferGraph:
    """Directed account graph over a sliding time window.

    Single-threaded, matching the simulation engine.
    """

    def __init__(self, window_ns: float = GRAPH_WINDOW_NS) -> None:
        self._window_ns = window_ns
        self._edges: deque[Edge] = deque()
        self._out: dict[str, dict[str, list[Edge]]] = defaultdict(lambda: defaultdict(list))
        self._in: dict[str, dict[str, list[Edge]]] = defaultdict(lambda: defaultdict(list))
        self._totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])

    def __len__(self) -> int:
        return len(self._edges)

    @property
    def account_count(self) -> int:
        return len({a for a in self._out} | {a for a in self._in})

    def add(self, source: str, destination: str, amount_paise: int,
            at_ns: float) -> None:
        """Record a transfer and drop anything that has fallen out of the window.

        Eviction happens on write rather than on a timer so the graph can never
        answer a query from data older than the window - which would silently
        widen the very filter the window exists to impose.
        """
        self._evict(at_ns)
        if not source or not destination or source == destination:
            # A self-transfer moves no money between parties and would create a
            # one-node cycle that matches every cycle test trivially.
            return

        edge = Edge(source, destination, amount_paise, at_ns)
        self._edges.append(edge)
        self._out[source][destination].append(edge)
        self._in[destination][source].append(edge)

        sent = self._totals[source]
        sent[0] += amount_paise
        sent[1] += 1
        received = self._totals[destination]
        received[2] += amount_paise
        received[3] += 1

    def structure(self, account_id: str, now_ns: float) -> AccountStructure:
        """Everything the graph can say about one account, right now."""
        totals = self._totals.get(account_id, [0, 0, 0, 0])
        shortest, count, fastest = self._cycles_through(account_id, now_ns)
        return AccountStructure(
            out_degree=len(self._out.get(account_id, {})),
            in_degree=len(self._in.get(account_id, {})),
            sent_total=totals[0],
            sent_count=totals[1],
            received_total=totals[2],
            received_count=totals[3],
            cycle_count=count,
            shortest_cycle=shortest,
            fastest_cycle_hours=fastest,
            fan_out_burst=self._burst(self._out, account_id, now_ns),
            fan_in_burst=self._burst(self._in, account_id, now_ns),
        )

    def _evict(self, now_ns: float) -> None:
        cutoff = now_ns - self._window_ns
        while self._edges and self._edges[0].at_ns < cutoff:
            edge = self._edges.popleft()
            self._drop(self._out, edge.source, edge.destination, edge)
            self._drop(self._in, edge.destination, edge.source, edge)

            sent = self._totals[edge.source]
            sent[0] -= edge.amount_paise
            sent[1] -= 1
            received = self._totals[edge.destination]
            received[2] -= edge.amount_paise
            received[3] -= 1

    @staticmethod
    def _drop(index: dict[str, dict[str, list[Edge]]], key: str, other: str,
              edge: Edge) -> None:
        bucket = index.get(key, {}).get(other)
        if not bucket:
            return
        bucket.remove(edge)
        if not bucket:
            del index[key][other]
            if not index[key]:
                del index[key]

    def _burst(self, index: dict[str, dict[str, list[Edge]]], account_id: str,
               now_ns: float) -> int:
        """Distinct counterparties inside the tight scatter-gather window.

        Fan-out over a month is a business. Fan-out over six hours to accounts
        that have never been paid before is a structuring pattern.
        """
        cutoff = now_ns - SCATTER_GATHER_WINDOW_NS
        return sum(
            1 for edges in index.get(account_id, {}).values()
            if any(e.at_ns >= cutoff for e in edges)
        )

    def _cycles_through(self, account_id: str,
                        now_ns: float) -> tuple[int, int, float]:
        """Tight cycles returning to this account.

        Bounded breadth-first search forward from the account, looking for a
        path back to it within `MAX_CYCLE_LENGTH` hops where every edge falls
        inside the cycle window. Returns the shortest length found, how many
        distinct first-hops closed a cycle, and the fastest closing time in
        hours.

        Bounded in three ways, all of which matter on a hub account: the hop
        limit, the node budget, and the time window on every edge. Without them
        one transfer through a busy account walks most of the graph.
        """
        cutoff = now_ns - CYCLE_WINDOW_NS
        neighbours = self._out.get(account_id)
        if not neighbours:
            return 0, 0, 0.0

        shortest = 0
        closed = 0
        fastest = 0.0
        visited_budget = MAX_CYCLE_SEARCH_NODES

        for first_hop, edges in neighbours.items():
            start = min((e.at_ns for e in edges if e.at_ns >= cutoff), default=None)
            if start is None:
                continue

            # One search per first hop, so `closed` counts distinct routes out
            # of this account that come back, not distinct paths - which would
            # explode combinatorially on a dense graph.
            frontier = deque([(first_hop, 1)])
            seen = {account_id, first_hop}
            found_length = 0

            while frontier and visited_budget > 0:
                node, depth = frontier.popleft()
                visited_budget -= 1
                if depth >= MAX_CYCLE_LENGTH:
                    continue

                for nxt, out_edges in self._out.get(node, {}).items():
                    last = max((e.at_ns for e in out_edges if e.at_ns >= cutoff),
                               default=None)
                    if last is None or last < start:
                        # The cycle has to close *after* it opened. Without this
                        # an older edge in the other direction counts as a
                        # return leg and every reciprocal pair looks like a
                        # cycle.
                        continue
                    if nxt == account_id:
                        found_length = depth + 1
                        fastest_here = (last - start) / NANOS_PER_HOUR
                        if fastest == 0.0 or fastest_here < fastest:
                            fastest = fastest_here
                        break
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append((nxt, depth + 1))

                if found_length:
                    break

            if found_length:
                closed += 1
                if shortest == 0 or found_length < shortest:
                    shortest = found_length

        return shortest, closed, fastest
