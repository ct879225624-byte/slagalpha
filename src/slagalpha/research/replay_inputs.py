"""Bounded DEV data requests; no downloading, rule approval, or replay execution."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.backtest.replay import TradeReplayRequest
from slagalpha.domain.universe import ContractRegistry, RegistryVerification, UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.parameters import DevParameterVersion, require_parameter_plan_binding
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import DatasetRole, ResearchSplitManifest
from slagalpha.strategy.plans import TakeProfitEvaluation, build_entry_stop

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReplayDataInputError(ValueError):
    """The declared plan or evidence cannot support a bounded DEV request."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def replay_data_bounds(request: TradeReplayRequest) -> tuple[datetime, datetime]:
    """Include the last possible entry's TIME_EXIT candle; never use future trade outcomes."""
    start = request.armed.confirmation_close
    expires = request.armed.expires_at
    if any(value.tzinfo is None or value.utcoffset() != timedelta(0)
           or value.minute % 15 or value.second or value.microsecond
           for value in (start, expires)):
        raise ReplayDataInputError("request boundaries must align to closed UTC 15m bars")
    if expires - start not in tuple(timedelta(minutes=15 * bars) for bars in (2, 4, 6)):
        raise ReplayDataInputError("request expiry must use an allowed Entry TTL")
    last_entry = expires - timedelta(minutes=1)
    last_bucket = last_entry.replace(minute=(last_entry.minute // 15) * 15)
    end = last_bucket + timedelta(minutes=15 * request.max_holding_bars + 1)
    return start, end


class DevReplayDataRequest(BaseModel):
    """Content binding and a finite data range, not proof that all execution gates passed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-replay-data-request/0.1.0"] = "dev-replay-data-request/0.1.0"
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    split_hash: Sha256
    sensitivity_plan_hash: Sha256
    parameter_content_hash: Sha256
    trade_plan_hash: Sha256
    universe_content_hash: Sha256
    registry_content_hash: Sha256
    rule_content_hash: Sha256
    request: TradeReplayRequest
    start: datetime
    end_exclusive: datetime
    expected_candle_count: int = Field(gt=0, strict=True)
    download_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    request_hash: Sha256

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.request.armed.symbol == "UNKNOWN":
            raise ValueError("data request must identify an exact symbol")
        if (self.start, self.end_exclusive) != replay_data_bounds(self.request):
            raise ValueError("data request must retain its complete bounded replay window")
        minutes = int((self.end_exclusive - self.start).total_seconds() / 60)
        if self.expected_candle_count != minutes:
            raise ValueError("data request candle count does not match window")
        if self.request_hash != _hash(self.model_dump(mode="json", exclude={"request_hash"})):
            raise ValueError("data request content hash mismatch")
        return self


def build_dev_replay_data_request(
    *, trade_plan: TakeProfitEvaluation, plan: SensitivityPlan, parameter: DevParameterVersion,
    split: ResearchSplitManifest, universe: UniverseSnapshot, registry: ContractRegistry,
) -> DevReplayDataRequest:
    """Reject blocked plans or incomplete rule horizons before any market-data acquisition."""
    trade_plan = TakeProfitEvaluation.model_validate(trade_plan.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    universe = UniverseSnapshot.model_validate(universe.model_dump(mode="json"))
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    if plan.blockers:
        raise ReplayDataInputError("DEV sensitivity plan is still blocked")
    if (split.split_hash != plan.split_hash
        or registry.registry_version != plan.contract_registry_version):
        raise ReplayDataInputError("split or registry does not belong to the research plan")
    params = parameter.candidate.parameters
    entry_stop = trade_plan.entry_stop
    expected_entry_stop = build_entry_stop(
        entry_stop.request, stop_atr_multiplier=params.stop_atr_multiplier,
        entry_ttl_bars=params.entry_ttl_bars,
    )
    if not trade_plan.accepted or not entry_stop.accepted or entry_stop != expected_entry_stop:
        raise ReplayDataInputError("Trade Plan must be accepted and match candidate Entry/Stop")
    request = TradeReplayRequest.from_take_profit(
        trade_plan, max_holding_bars=params.max_holding_bars,
    )
    start, end = replay_data_bounds(request)
    dev = next(item for item in split.segments if item.role is DatasetRole.DEV)
    if not (datetime.combine(dev.start, time(), UTC) <= start
            and end <= datetime.combine(dev.end_exclusive, time(), UTC)):
        raise ReplayDataInputError("complete replay window must remain inside DEV")
    if not (universe.effective_from <= start < universe.effective_to):
        raise ReplayDataInputError("Universe snapshot is inactive at confirmation")
    symbol = request.armed.symbol
    if symbol not in {member.symbol for member in universe.members}:
        raise ReplayDataInputError("signal is not a member of the historical Universe")
    rules = tuple(item for item in registry.entries if item.symbol == symbol
                  and item.effective_from <= start
                  and (item.effective_to is None or end <= item.effective_to))
    if len(rules) != 1 or rules[0].verification_status is not RegistryVerification.VERIFIED:
        raise ReplayDataInputError(
            "one VERIFIED historical rule must cover the complete replay window"
        )
    rule = rules[0]
    if rule.tick_size != request.tick_size:
        raise ReplayDataInputError("Trade Plan tick size does not match the historical rule")
    if (rule.status != "TRADING"
        or (rule.onboard_date is not None and rule.onboard_date > start)
        or rule.derived_first_candle_at > start
        or (rule.inferred_delisted_at is not None and rule.inferred_delisted_at < end)):
        raise ReplayDataInputError("contract lifecycle does not cover the complete replay window")
    payload = {
        "schema_version": "dev-replay-data-request/0.1.0", "dataset_role": "DEV",
        "split_hash": split.split_hash, "sensitivity_plan_hash": plan.plan_hash,
        "parameter_content_hash": parameter.content_hash,
        "trade_plan_hash": _hash(trade_plan.model_dump(mode="json")),
        "universe_content_hash": _hash(universe.model_dump(mode="json")),
        "registry_content_hash": _hash(registry.model_dump(mode="json")),
        "rule_content_hash": _hash(rule.model_dump(mode="json")),
        "request": request.model_dump(mode="json"), "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "expected_candle_count": int((end - start).total_seconds() / 60),
        "download_authorized": False, "research_authorized": False,
    }
    # Pydantic normalizes UTC timestamps to Z before hashing the canonical model form.
    payload["start"] = start.isoformat().replace("+00:00", "Z")
    payload["end_exclusive"] = end.isoformat().replace("+00:00", "Z")
    return DevReplayDataRequest.model_validate({**payload, "request_hash": _hash(payload)})


def require_replay_data_request_binding(
    request: DevReplayDataRequest, *, trade_plan: TakeProfitEvaluation, plan: SensitivityPlan,
    parameter: DevParameterVersion, split: ResearchSplitManifest, universe: UniverseSnapshot,
    registry: ContractRegistry,
) -> None:
    """Re-evaluate original evidence; a saved request or matching hash is not permission."""
    observed = build_dev_replay_data_request(
        trade_plan=trade_plan, plan=plan, parameter=parameter, split=split,
        universe=universe, registry=registry,
    )
    if observed != request:
        raise ReplayDataInputError("saved data request does not match current source evidence")


def write_dev_replay_data_request(request: DevReplayDataRequest, data_dir: Path) -> Path:
    request = DevReplayDataRequest.model_validate(request.model_dump(mode="json"))
    destination = data_dir / "manifests" / "replay_data_request" / f"{request.request_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ReplayDataInputError("request output path escapes data directory")
    content = canonical_json_bytes(request.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ReplayDataInputError("existing data request changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
