"""The authoritative shape of data crossing the streamHub boundary from
xWeb's Model.Core - MarketWorkflowResult/TriggerMetrics/SaxoChartSample/
MarketSignalEntry mirrored field-for-field from xWeb's own source
(confirmed directly with the xWeb session, not inferred from this add-on's
own historical dict.get() call sites). SaxoAuthStatus is the one
exception: it has no REST endpoint anywhere in xWeb, so nothing reflects
it into an OpenAPI schema, and there is no authoritative source beyond
this add-on's own observed field usage (Authenticated, PublicLoginUrl) -
hand-maintained, not derived, until xWeb adds a route for it (tracked in
xWeb's own action plan, not this repo's).

Both hub topics serialize via JsonConvert.SerializeObject (Newtonsoft,
no [JsonProperty] overrides) - PascalCase wire keys matching the C#
property names exactly. That is NOT the same casing xWeb's REST
endpoints use (System.Text.Json default, camelCase) - a model built
against this file's aliases only validates hub-broadcast bytes, not a
REST response body, and should never be pointed at one.

Once xWeb's IResult-typing gap is fixed (MarketWorkflowResult/
TriggerMetrics currently can't appear in xWeb's OpenAPI doc because both
mapped endpoints return bare IResult - tracked in xWeb's plan) these
classes become a real code-generation target and this file becomes the
thing a generator replaces, not something to keep growing by hand.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _parse_dotnet_dt(v):
    """Newtonsoft's UTC DateTime wire format: a trailing Z and up to 7
    fractional-second digits (.NET ticks, 100ns precision) - one more
    digit than Python's datetime parsing tolerates. Truncate to
    microseconds rather than assume a fixed precision, same approach
    ui.py's own _parse_dotnet_dt already uses for this exact quirk.
    """
    if v is None or isinstance(v, datetime):
        return v
    s = str(v).rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


DotNetDateTime = Annotated[datetime, BeforeValidator(_parse_dotnet_dt)]
OptionalDotNetDateTime = Annotated[datetime | None, BeforeValidator(_parse_dotnet_dt)]


class _HubModel(BaseModel):
    """Base for anything arriving over a streamHub broadcast: PascalCase
    wire aliases, but constructible/accessible by the Pythonic snake_case
    name too (populate_by_name) - callers write result.public_login_url,
    not result.PublicLoginUrl, without losing the ability to validate the
    exact bytes the hub actually sends.
    """
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SaxoChartSample(_HubModel):
    """One EvalCandles entry. Carries both the plain-OHLC fields (CFDs,
    stocks) and the Bid/Ask variants (FxSpot) - which set is populated
    depends on instrument type, not both together, so every field here
    is optional even though exactly one family will actually be set for
    a given instrument.
    """
    time: DotNetDateTime = Field(alias="Time")
    open: Decimal | None = Field(default=None, alias="Open")
    high: Decimal | None = Field(default=None, alias="High")
    low: Decimal | None = Field(default=None, alias="Low")
    close: Decimal | None = Field(default=None, alias="Close")
    open_bid: Decimal | None = Field(default=None, alias="OpenBid")
    open_ask: Decimal | None = Field(default=None, alias="OpenAsk")
    high_bid: Decimal | None = Field(default=None, alias="HighBid")
    high_ask: Decimal | None = Field(default=None, alias="HighAsk")
    low_bid: Decimal | None = Field(default=None, alias="LowBid")
    low_ask: Decimal | None = Field(default=None, alias="LowAsk")
    close_bid: Decimal | None = Field(default=None, alias="CloseBid")
    close_ask: Decimal | None = Field(default=None, alias="CloseAsk")
    volume: Decimal | None = Field(default=None, alias="Volume")
    sma50: Decimal | None = Field(default=None, alias="Sma50")
    sma200: Decimal | None = Field(default=None, alias="Sma200")


class MarketSignalEntry(_HubModel):
    """MarketWorkflowResult.Signal - populated only on a Completed tick."""
    date: DotNetDateTime = Field(alias="Date")
    question: str | None = Field(default=None, alias="Question")
    answer: str | None = Field(default=None, alias="Answer")
    symbols: list[str] | None = Field(default=None, alias="Symbols")


class TriggerMetrics(_HubModel):
    current_price: Decimal = Field(alias="CurrentPrice")
    current_volatility_percent: Decimal = Field(alias="CurrentVolatilityPercent")
    baseline_price: Decimal = Field(alias="BaselinePrice")
    baseline_volatility: Decimal = Field(alias="BaselineVolatility")
    price_move_percent: Decimal = Field(alias="PriceMovePercent")
    volatility_move_percent: Decimal = Field(alias="VolatilityMovePercent")
    avg_volatility_percent: Decimal = Field(alias="AvgVolatilityPercent")
    avg_volume: Decimal | None = Field(default=None, alias="AvgVolume")
    triggered: bool = Field(alias="Triggered")
    reasons: list[str] | None = Field(default=None, alias="Reasons")
    price_move_threshold_percent: Decimal = Field(alias="PriceMoveThresholdPercent")
    volatility_threshold_percent: Decimal = Field(alias="VolatilityThresholdPercent")
    # non-null only on the tick that (re)set the baseline
    new_baseline_price: Decimal | None = Field(default=None, alias="NewBaselinePrice")
    new_baseline_volatility: Decimal | None = Field(default=None, alias="NewBaselineVolatility")


class MarketWorkflowResult(_HubModel):
    """marketagent.preview's payload. Field presence varies by status -
    see each field's comment - not a schema violation, this mirrors
    Model.Core's own reference types being un-null-annotated (presence
    is a runtime contract, not a compile-time one, on xWeb's side
    either).
    """
    status: str = Field(alias="Status")  # SaxoAuthRequired|MarketClosed|TriggerNotMet|Preview|Completed
    run_at: DotNetDateTime = Field(alias="RunAt")
    public_login_url: str | None = Field(default=None, alias="PublicLoginUrl")  # present every tick
    next_retry_after: OptionalDotNetDateTime = Field(default=None, alias="NextRetryAfter")
    metrics: TriggerMetrics | None = Field(default=None, alias="Metrics")
    is_market_open: bool | None = Field(default=None, alias="IsMarketOpen")  # Preview-only
    eval_candles: list[SaxoChartSample] | None = Field(default=None, alias="EvalCandles")  # Preview-only
    system_prompt: str | None = Field(default=None, alias="SystemPrompt")  # Preview-only
    user_content: str | None = Field(default=None, alias="UserContent")  # Preview-only
    signal: MarketSignalEntry | None = Field(default=None, alias="Signal")  # Completed-only
    input_tokens: int | None = Field(default=None, alias="InputTokens")  # Completed-only
    output_tokens: int | None = Field(default=None, alias="OutputTokens")  # Completed-only
    model: str | None = Field(default=None, alias="Model")  # Completed-only


class SaxoAuthStatus(_HubModel):
    """saxo.authstatus's payload - hand-maintained, see module docstring.
    Only the two fields this add-on actually reads exist here; extend
    this (not a fresh dict.get() call site) if a third is ever needed.
    """
    authenticated: bool = Field(alias="Authenticated")
    public_login_url: str | None = Field(default=None, alias="PublicLoginUrl")
