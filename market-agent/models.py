"""The authoritative shape of data crossing the streamHub boundary from
xWeb's Model.Core. Cross-checked against xWeb's own OpenAPI doc
(generated from a dev run of Services.API after xWeb retyped
GET/POST /claude/MarketAgent from bare IResult to
Results<Ok<MarketWorkflowResult>, ProblemHttpResult> and added
GET /saxo/AuthStatus - both were IResult-typed or missing entirely
before, which is why every field here used to be hand-transcribed from
Model.Core's C# source instead of generated) - not a raw
datamodel-code-generator drop-in, because two things it produces aren't
right for this file's actual job:

- It only sees REST's camelCase - every field here instead validates
  BOTH casings via AliasChoices, since the hub broadcasts the same
  classes via JsonConvert.SerializeObject (Newtonsoft, PascalCase, no
  [JsonProperty] overrides) while REST responses use System.Text.Json's
  default camelCase. Serialization always writes PascalCase - the hub
  is this add-on's primary channel today.
- It renders C# decimal as `float | constr(pattern=...)` (defensive
  precision handling in the OpenAPI schema itself). Pydantic's Decimal
  type already accepts both a JSON number and a numeric string
  natively, so it's the more precise AND simpler choice for financial
  fields - no reason to carry the generator's union verbatim.

Regenerate by re-fetching http://localhost:8080/openapi/v1.json from a
dev run of Services.API and diffing components.schemas against this
file - field names/nullability should track that doc, not the other
way around. The two adjustments above are deliberate and should survive
a regeneration, not get discarded because a fresh generator run doesn't
know about them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field


def _parse_dotnet_dt(v):
    """Newtonsoft's UTC DateTime wire format: a trailing Z and up to 7
    fractional-second digits (.NET ticks, 100ns precision) - one more
    digit than Python's datetime parsing tolerates. Truncate to
    microseconds rather than assume a fixed precision, same approach
    ui.py's own _parse_dotnet_dt already uses for this exact quirk.
    Only applies to hub-format strings (System.Text.Json's REST output
    already round-trips through Pydantic's own ISO 8601 parsing fine) -
    a value that's already a datetime (or None) passes through untouched.
    """
    if v is None or isinstance(v, datetime):
        return v
    s = str(v).rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return v  # let Pydantic's own datetime parser take a shot (REST/offset-aware ISO)


DotNetDateTime = Annotated[datetime, BeforeValidator(_parse_dotnet_dt)]
OptionalDotNetDateTime = Annotated[datetime | None, BeforeValidator(_parse_dotnet_dt)]


def _dual(pascal: str, camel: str, **kwargs):
    """One field, two wire casings - the hub's PascalCase and REST's
    camelCase both validate; serialization always writes PascalCase
    (the hub is the channel this add-on actually uses today)."""
    return Field(validation_alias=AliasChoices(pascal, camel), serialization_alias=pascal, **kwargs)


class _HubModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SaxoChartSample(_HubModel):
    """One EvalCandles entry. Carries both the plain-OHLC fields (CFDs,
    stocks) and the Bid/Ask variants (FxSpot) - which set is populated
    depends on instrument type, not both together, so every field here
    is optional even though exactly one family will actually be set for
    a given instrument.
    """
    time: DotNetDateTime = _dual("Time", "time")
    open: Decimal | None = _dual("Open", "open", default=None)
    high: Decimal | None = _dual("High", "high", default=None)
    low: Decimal | None = _dual("Low", "low", default=None)
    close: Decimal | None = _dual("Close", "close", default=None)
    open_bid: Decimal | None = _dual("OpenBid", "openBid", default=None)
    open_ask: Decimal | None = _dual("OpenAsk", "openAsk", default=None)
    high_bid: Decimal | None = _dual("HighBid", "highBid", default=None)
    high_ask: Decimal | None = _dual("HighAsk", "highAsk", default=None)
    low_bid: Decimal | None = _dual("LowBid", "lowBid", default=None)
    low_ask: Decimal | None = _dual("LowAsk", "lowAsk", default=None)
    close_bid: Decimal | None = _dual("CloseBid", "closeBid", default=None)
    close_ask: Decimal | None = _dual("CloseAsk", "closeAsk", default=None)
    volume: Decimal | None = _dual("Volume", "volume", default=None)
    sma50: Decimal | None = _dual("Sma50", "sma50", default=None)
    sma200: Decimal | None = _dual("Sma200", "sma200", default=None)


class MarketSignalEntry(_HubModel):
    """MarketWorkflowResult.Signal - populated only on a Completed tick."""
    date: DotNetDateTime = _dual("Date", "date")
    question: str | None = _dual("Question", "question", default=None)
    answer: str | None = _dual("Answer", "answer", default=None)
    symbols: list[str] | None = _dual("Symbols", "symbols", default=None)


class TriggerMetrics(_HubModel):
    current_price: Decimal = _dual("CurrentPrice", "currentPrice")
    current_volatility_percent: Decimal = _dual("CurrentVolatilityPercent", "currentVolatilityPercent")
    baseline_price: Decimal = _dual("BaselinePrice", "baselinePrice")
    baseline_volatility: Decimal = _dual("BaselineVolatility", "baselineVolatility")
    price_move_percent: Decimal = _dual("PriceMovePercent", "priceMovePercent")
    volatility_move_percent: Decimal = _dual("VolatilityMovePercent", "volatilityMovePercent")
    avg_volatility_percent: Decimal = _dual("AvgVolatilityPercent", "avgVolatilityPercent")
    avg_volume: Decimal | None = _dual("AvgVolume", "avgVolume", default=None)
    triggered: bool = _dual("Triggered", "triggered")
    reasons: list[str] | None = _dual("Reasons", "reasons", default=None)
    price_move_threshold_percent: Decimal = _dual("PriceMoveThresholdPercent", "priceMoveThresholdPercent")
    volatility_threshold_percent: Decimal = _dual("VolatilityThresholdPercent", "volatilityThresholdPercent")
    # non-null only on the tick that (re)set the baseline
    new_baseline_price: Decimal | None = _dual("NewBaselinePrice", "newBaselinePrice", default=None)
    new_baseline_volatility: Decimal | None = _dual("NewBaselineVolatility", "newBaselineVolatility", default=None)


class MarketWorkflowResult(_HubModel):
    """marketagent.preview's payload, and GET/POST /claude/MarketAgent's
    REST response - the same C# class, two channels. Field presence
    varies by status (see each field's comment) - mirrors Model.Core's
    own reference types being un-null-annotated, a runtime contract on
    xWeb's side too, not just here.
    """
    status: str = _dual("Status", "status")  # SaxoAuthRequired|MarketClosed|TriggerNotMet|Preview|Completed
    run_at: DotNetDateTime = _dual("RunAt", "runAt")
    public_login_url: str | None = _dual("PublicLoginUrl", "publicLoginUrl", default=None)  # present every tick
    next_retry_after: OptionalDotNetDateTime = _dual("NextRetryAfter", "nextRetryAfter", default=None)
    metrics: TriggerMetrics | None = _dual("Metrics", "metrics", default=None)
    is_market_open: bool | None = _dual("IsMarketOpen", "isMarketOpen", default=None)  # Preview-only
    eval_candles: list[SaxoChartSample] | None = _dual("EvalCandles", "evalCandles", default=None)  # Preview-only
    system_prompt: str | None = _dual("SystemPrompt", "systemPrompt", default=None)  # Preview-only
    user_content: str | None = _dual("UserContent", "userContent", default=None)  # Preview-only
    signal: MarketSignalEntry | None = _dual("Signal", "signal", default=None)  # Completed-only
    input_tokens: int | None = _dual("InputTokens", "inputTokens", default=None)  # Completed-only
    output_tokens: int | None = _dual("OutputTokens", "outputTokens", default=None)  # Completed-only
    model: str | None = _dual("Model", "model", default=None)  # Completed-only


class SaxoAuthStatus(_HubModel):
    """saxo.authstatus's payload, and GET /saxo/AuthStatus's REST
    response (added specifically to make this class OpenAPI-visible -
    xWeb commit 03106a1). updated_at/refresh_token_expires_at were
    missing from this add-on's own field usage entirely until generated
    against the real schema - exactly the kind of drift this file exists
    to catch; neither is consumed yet, but both are declared so a future
    caller gets real validation instead of a fresh dict.get() call site.
    """
    updated_at: OptionalDotNetDateTime = _dual("UpdatedAt", "updatedAt", default=None)
    authenticated: bool = _dual("Authenticated", "authenticated")
    refresh_token_expires_at: OptionalDotNetDateTime = _dual(
        "RefreshTokenExpiresAt", "refreshTokenExpiresAt", default=None)
    public_login_url: str | None = _dual("PublicLoginUrl", "publicLoginUrl", default=None)
