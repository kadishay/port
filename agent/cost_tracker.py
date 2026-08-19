from dataclasses import dataclass, field

_RATES = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-opus-4-8":  {"input": 5.00, "output": 25.00},
}

def _normalize(model: str) -> str:
    for key in _RATES:
        if model.startswith(key):
            return key
    return model


@dataclass
class CostTracker:
    _input: dict[str, int] = field(default_factory=dict)
    _output: dict[str, int] = field(default_factory=dict)

    def record(self, model: str, usage) -> None:
        key = _normalize(model)
        self._input[key] = self._input.get(key, 0) + usage.input_tokens
        self._output[key] = self._output.get(key, 0) + usage.output_tokens

    def models_used(self) -> set[str]:
        return set(self._input) | set(self._output)

    def total_cost(self) -> float:
        total = 0.0
        for model in set(list(self._input) + list(self._output)):
            rates = _RATES.get(model, {"input": 0, "output": 0})
            total += self._input.get(model, 0) * rates["input"] / 1_000_000
            total += self._output.get(model, 0) * rates["output"] / 1_000_000
        return total

    def summary(self) -> str:
        lines = ["💰 *Cost breakdown:*"]
        total = 0.0
        for model in sorted(set(list(self._input) + list(self._output))):
            rates = _RATES.get(model, {"input": 0, "output": 0})
            cost = (
                self._input.get(model, 0) * rates["input"] +
                self._output.get(model, 0) * rates["output"]
            ) / 1_000_000
            total += cost
            lines.append(
                f"  {model}: {self._input.get(model,0):,} in / "
                f"{self._output.get(model,0):,} out → ${cost:.4f}"
            )
        lines.append(f"  *Total: ${total:.4f}*")
        return "\n".join(lines)
