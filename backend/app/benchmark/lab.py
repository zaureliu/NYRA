"""Model Benchmark Lab (spec Parte K-Q, AZ, BA).

Reproduzível e isolado:

* benchmark NUNCA altera o modelo oficial (§67) nem baixa modelos (§70);
* modelo ausente retorna estado válido ``MODEL_NOT_INSTALLED`` (§69, §100);
* métricas de performance: cold/warm load, TTFT, tokens/s, prompt_eval,
  eval_duration, total_duration, RAM, VRAM por contexto (2048/4096/8192);
* mediana sempre; p95 quando amostra suficiente (§76-§78);
* quality benchmark das tarefas REAIS da NYRA com scoring DETERMINÍSTICO —
  nunca o mesmo LLM como único juiz (§90);
* runs executam em background para não travar o chat (§227);
* comparação futura 8B×14B e promotion gate manual-only (§102-§107).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.paths import DATA_ROOT

logger = logging.getLogger("nyra.benchmark")

BENCHMARK_ROOT = DATA_ROOT / "model-benchmarks"
BASELINES_DIR = BENCHMARK_ROOT / "baselines"

DEFAULT_CONTEXTS = (2048, 4096, 8192)
PERF_REPEATS_DEFAULT = 3

# Perfis de candidato futuro — NÃO assumem nome exato nem instalação (§98-§101).
MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "qwen-14b-candidate": {
        "profile_id": "qwen-14b-candidate",
        "label": "Qwen 14B candidate",
        "match": re.compile(r"^qwen[^:]*:?14b", re.IGNORECASE),
        "note": "Perfil genérico para o futuro upgrade 14B; aceita qualquer tag qwen 14B instalada.",
    },
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _p95(values: list[float]) -> float | None:
    if len(values) < 4:
        return None  # p95 só com amostra suficiente (§78)
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return round(ordered[index], 2)


class ModelNotInstalled(Exception):
    def __init__(self, model_id: str) -> None:
        super().__init__(f"MODEL_NOT_INSTALLED:{model_id}")
        self.model_id = model_id


class BenchmarkRunRegistry:
    """In-memory tracking so benchmarks never block the chat pipeline (§227)."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def register(self, run_id: str, kind: str, payload: dict) -> None:
        self._runs[run_id] = {
            "run_id": run_id,
            "kind": kind,
            "state": "QUEUED",
            "created_at": _utcnow_iso(),
            "request": payload,
            "result": None,
            "error_code": None,
        }

    def attach(self, run_id: str, task: asyncio.Task) -> None:
        self._tasks[run_id] = task
        entry = self._runs.get(run_id)
        if entry:
            entry["state"] = "RUNNING"
            entry["started_at"] = _utcnow_iso()

    def finish(self, run_id: str, result: dict) -> None:
        entry = self._runs.get(run_id)
        if entry is None:
            return
        if result.pop("_failed", False) or result.get("error_code"):
            entry["state"] = "FAILED"
            entry["error_code"] = result.get("error_code") or "RUN_FAILED"
        else:
            entry["state"] = "DONE"
        entry["result"] = result
        entry["finished_at"] = _utcnow_iso()

    def get(self, run_id: str) -> dict | None:
        entry = self._runs.get(run_id)
        if entry is None:
            return None
        snapshot = json.loads(json.dumps(entry, ensure_ascii=False))
        task = self._tasks.get(run_id)
        if task and task.done() and snapshot["state"] == "RUNNING":
            error = task.exception()
            if error:
                snapshot["state"] = "FAILED"
                snapshot["error_code"] = f"CRASH:{type(error).__name__}"
        return snapshot

    def list(self) -> list[dict]:
        return [self.get(run_id) or {} for run_id in list(self._runs)]


class ModelBenchmarkLab:
    def __init__(self, base_url: str, *, brain=None, settings=None,
                 registry: BenchmarkRunRegistry | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.brain = brain
        self.settings = settings
        self.registry = registry or BenchmarkRunRegistry()
        BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
        BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    async def installed_models(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            return response.json().get("models", [])

    async def require_installed(self, model_id: str) -> dict:
        for item in await self.installed_models():
            if item.get("name") == model_id:
                return item
        raise ModelNotInstalled(model_id)

    async def resolve_profile(self, profile_or_model: str) -> dict:
        """Resolve a model id or future-profile id against installed models."""
        installed_names = [item.get("name") for item in await self.installed_models()]
        profile = MODEL_PROFILES.get(profile_or_model)
        if profile:
            resolved = next((name for name in installed_names
                             if name and profile["match"].match(name)), None)
            return {**profile, "installed": resolved is not None,
                    "resolved_model": resolved,
                    "display_state": "INSTALLED" if resolved else "NOT INSTALLED"}
        installed = profile_or_model in installed_names
        return {"profile_id": None, "model_id": profile_or_model,
                "installed": installed,
                "display_state": "INSTALLED" if installed else "NOT INSTALLED"}

    async def profiles_overview(self) -> dict:
        current_official = getattr(self.brain, "official_model", None) \
            or getattr(self.settings, "llm_model", None)
        candidates = []
        for profile_id, profile in MODEL_PROFILES.items():
            resolved = await self.resolve_profile(profile_id)
            candidates.append({
                "profile_id": profile_id,
                "label": profile["label"],
                "installed": resolved["installed"],
                "resolved_model": resolved.get("resolved_model"),
                "display_state": resolved["display_state"],
                "benchmark_ready": True,  # ausência é estado válido, não erro (§100/§101)
            })
        return {
            "current_official_model": current_official,
            "active_model": getattr(self.brain, "model", current_official),
            "candidates": candidates,
            "generated_at": _utcnow_iso(),
        }

    # ------------------------------------------------------------------- storage
    def persist_run(self, document: dict) -> Path:
        BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
        path = BENCHMARK_ROOT / f"{document['run_id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return path

    def save_baseline(self, run_id: str, label: str) -> dict:
        source = BENCHMARK_ROOT / f"{run_id}.json"
        if not source.exists():
            return {"success": False, "error_code": "RUN_NOT_FOUND"}
        document = json.loads(source.read_text(encoding="utf-8"))
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-").lower() or "baseline"
        target = BASELINES_DIR / f"{safe_label}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
        return {"success": True, "label": safe_label, "path": str(target)}

    def load_baseline_document(self, label: str) -> dict | None:
        path = BASELINES_DIR / f"{Path(label).stem}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list_baselines(self) -> list[dict]:
        items = []
        if BASELINES_DIR.exists():
            for path in sorted(BASELINES_DIR.glob("*.json")):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    items.append({
                        "label": path.stem,
                        "run_id": document.get("run_id"),
                        "model_id": document.get("model_id"),
                        "created_at": document.get("created_at"),
                        "has_perf": bool(document.get("perf")),
                        "has_quality": bool(document.get("quality")),
                    })
                except (OSError, ValueError):
                    continue
        return items

    # ---------------------------------------------------------------- background
    def start_run(self, kind: str, *, model_id: str, contexts: list[int] | None = None,
                  repeats: int = PERF_REPEATS_DEFAULT) -> dict:
        run_id = f"bm_{int(time.time_ns())}"
        request_payload = {"model_id": model_id, "contexts": contexts or [],
                           "repeats": repeats}
        self.registry.register(run_id, kind, request_payload)

        async def _runner() -> None:
            try:
                if kind == "perf":
                    result = await self.perf_run(model_id, contexts=contexts, repeats=repeats)
                    result["run_id"] = run_id
                    self.persist_run(result)
                elif kind == "quality":
                    result = await self.quality_run(model_id)
                    result["run_id"] = run_id
                    self.persist_run(result)
                elif kind == "full":
                    perf = await self.perf_run(model_id, contexts=contexts, repeats=repeats)
                    perf["run_id"] = run_id
                    quality = None
                    if not perf.get("error"):
                        quality = await self.quality_run(model_id)
                    merged = {**perf, "kind": "full", "quality": quality}
                    self.persist_run(merged)
                    result = merged
                else:
                    result = {"_failed": True, "error_code": "UNKNOWN_RUN_KIND"}
                self.registry.finish(run_id, result)
            except ModelNotInstalled as error:
                self.registry.finish(run_id, {"_failed": True,
                                              "error_code": "MODEL_NOT_INSTALLED",
                                              "model_id": error.model_id})
            except Exception as error:  # noqa: BLE001
                logger.exception("benchmark run crashed")
                self.registry.finish(run_id, {"_failed": True,
                                              "error_code": f"CRASH:{type(error).__name__}"})

        task = asyncio.create_task(_runner(), name=f"nyra-benchmark-{run_id}")
        self.registry.attach(run_id, task)
        return {"success": True, "run_id": run_id, "state": "QUEUED"}

    # -------------------------------------------------------------- performance
    async def perf_run(self, model_id: str, *,
                       contexts: list[int] | None = None,
                       repeats: int = PERF_REPEATS_DEFAULT) -> dict:
        await self.require_installed(model_id)
        repeats = max(1, min(int(repeats), 5))
        allowed = {1024, 2048, 4096, 8192, 16384}
        selected_contexts = [ctx for ctx in (contexts or []) if int(ctx) in allowed]
        if not selected_contexts:
            selected_contexts = list(DEFAULT_CONTEXTS)
        official_model = getattr(self.brain, "official_model", None) \
            or getattr(self.settings, "llm_model", None)

        document: dict[str, Any] = {
            "run_id": "",  # preenchido pelo chamador em background runs
            "kind": "perf",
            "model_id": model_id,
            "official_model_untouched": official_model,
            "created_at": _utcnow_iso(),
            "contexts": {},
        }
        cold_loads: list[float] = []
        for context in selected_contexts:
            cold = await self._measure_once(model_id, context, cold=True)
            if isinstance(cold.get("load_ms"), (int, float)):
                cold_loads.append(float(cold["load_ms"]))
            warm_runs = []
            for _ in range(repeats):
                warm_runs.append(await self._measure_once(model_id, context, cold=False))
            document["contexts"][str(context)] = {
                "cold": cold,
                "warm_runs": warm_runs,
                "warm_median": _median_of(warm_runs),
                "warm_p95": _p95_of(warm_runs),
            }

        vram = await self._vram_snapshot(model_id)
        ram = _ram_usage()
        ttft_medians = [_safe_median_value(document["contexts"][key]["warm_median"], "ttft_ms")
                        for key in document["contexts"]]
        tps_medians = [_safe_median_value(document["contexts"][key]["warm_median"],
                                          "tokens_per_second") for key in document["contexts"]]
        total_medians = [_safe_median_value(document["contexts"][key]["warm_median"], "total_ms")
                         for key in document["contexts"]]
        document.update({
            "vram_bytes_loaded": vram.get("size_vram"),
            "vram_total_bytes": vram.get("size_total"),
            "context_length_observed": vram.get("context_length"),
            "ram_used_bytes": ram.get("used_bytes"),
            "ram_percent": ram.get("percent"),
            "cold_load_ms_median": _median(cold_loads),
            "summary": {
                "ttft_ms_median_warm": _median(ttft_medians),
                "tokens_per_second_median_warm": _median(tps_medians),
                "total_ms_median_warm": _median(total_medians),
                "repeats_per_context": repeats,
                "contexts_tested": selected_contexts,
            },
        })
        await self._restore_production_state(official_model)
        return document

    async def _measure_once(self, model_id: str, context: int, *, cold: bool) -> dict:
        if cold:
            await self._unload(model_id)
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": "Você é a NYRA. Responda curto em português."},
                {"role": "user", "content": "Responda apenas: ok"},
            ],
            "stream": True,
            "think": False,
            "keep_alive": "15m",
            "options": {"temperature": 0, "num_predict": 12, "num_ctx": context},
        }
        started = time.perf_counter()
        first_token_at: float | None = None
        final: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat",
                                         json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        token = str(data.get("message", {}).get("content", ""))
                        if token and first_token_at is None:
                            first_token_at = time.perf_counter()
                        if data.get("done"):
                            final = data
                            break
        except Exception as error:  # noqa: BLE001
            return {"error": type(error).__name__, "cold": cold, "context": context}

        ended = time.perf_counter()
        ttft_ms = ((first_token_at or ended) - started) * 1000
        total_ms = (ended - started) * 1000
        eval_count = int(final.get("eval_count") or 0)
        prompt_eval_count = int(final.get("prompt_eval_count") or 0)
        eval_seconds = float(final.get("eval_duration") or 0) / 1e9
        prompt_eval_ms = float(final.get("prompt_eval_duration") or 0) / 1e6
        return {
            "cold": cold,
            "context": context,
            "load_ms": round(float(final.get("load_duration") or 0) / 1e6, 2),
            "ttft_ms": round(ttft_ms, 2),
            "total_ms": round(total_ms, 2),
            "prompt_eval_tokens": prompt_eval_count,
            "prompt_eval_ms": round(prompt_eval_ms, 2),
            "eval_tokens": eval_count,
            "eval_duration_ms": round(eval_seconds * 1000, 2),
            "tokens_per_second": round(eval_count / eval_seconds, 2) if eval_seconds > 0 else None,
            "total_duration_ms_server": round(float(final.get("total_duration") or 0) / 1e6, 2),
        }

    async def _unload(self, model_id: str) -> None:
        """Descarregamento controlado do modelo para o teste frio (§74)."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
                await client.post(f"{self.base_url}/api/generate",
                                  json={"model": model_id, "keep_alive": 0})
        except Exception:  # noqa: BLE001 - unload best-effort
            pass

    async def _restore_production_state(self, official_model: str | None) -> None:
        """Garante que o modelo OFICIAL volta residente após o benchmark (§67)."""
        if not official_model:
            return
        keep_alive = str(getattr(self.settings, "ollama_keep_alive", "1h") or "1h")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(360, connect=5)) as client:
                await client.post(f"{self.base_url}/api/generate",
                                  json={"model": official_model, "keep_alive": keep_alive})
        except Exception:  # noqa: BLE001
            logger.warning("benchmark could not restore warm state for %s", official_model)

    async def _vram_snapshot(self, model_id: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=6) as client:
                response = await client.get(f"{self.base_url}/api/ps")
                for item in response.json().get("models", []):
                    if item.get("name") == model_id:
                        return {"size_vram": item.get("size_vram"),
                                "size_total": item.get("size"),
                                "context_length": item.get("context_length")}
        except Exception:  # noqa: BLE001
            pass
        return {}

    # ------------------------------------------------------------------ quality
    async def quality_run(self, model_id: str) -> dict:
        await self.require_installed(model_id)
        results: list[dict] = []
        for case in QUALITY_CASES:
            results.append(await self._quality_case(model_id, case))
        passed = sum(1 for item in results if item["score"] >= 1.0)
        partial = sum(1 for item in results if 0.0 < item["score"] < 1.0)
        failed = sum(1 for item in results if item["score"] <= 0.0)
        buckets: dict[str, list[float]] = {}
        for item in results:
            buckets.setdefault(item["category"], []).append(item["score"])
        category_scores = {category: round(sum(values) / len(values), 3)
                           for category, values in buckets.items() if values}
        totals = {
            "cases": len(results),
            "passed": passed,
            "partial": partial,
            "failed": failed,
            "tool_accuracy": category_scores.get("tool_selection", 0.0),
            "multi_step_score": max(category_scores.get("multi_step", 0.0),
                                    category_scores.get("workflow", 0.0)),
            "grounding_score": min(category_scores.get("grounding", 0.0),
                                   category_scores.get("homelab", 1.0)),
            "recovery_score": category_scores.get("recovery", 0.0),
            "conversation_score": category_scores.get("conversation", 0.0),
            "overall": round(sum(item["score"] for item in results) / max(1, len(results)), 3),
        }
        return {
            "kind": "quality",
            "model_id": model_id,
            "created_at": _utcnow_iso(),
            "cases": results,
            "category_scores": category_scores,
            "totals": totals,
        }

    async def _chat_direct(self, model_id: str, messages: list[dict],
                           *, tools: list[dict] | None = None,
                           num_predict: int = 220) -> tuple[str, list[dict]]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "15m",
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": num_predict},
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10)) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        message = data.get("message", {})
        calls = [
            {"function": {"name": (call.get("function") or {}).get("name")},
             "arguments": (call.get("function") or {}).get("arguments")}
            for call in message.get("tool_calls", []) or []
        ]
        return str(message.get("content", "")), calls

    async def _quality_case(self, model_id: str, case: dict) -> dict:
        started = time.perf_counter()
        messages = [{"role": "system", "content": case.get("system") or SYSTEM_PROMPT_MIN},
                    {"role": "user", "content": case["prompt"]}]
        try:
            text, calls = await self._chat_direct(model_id, messages, tools=case.get("tools"))
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
        except Exception as error:  # noqa: BLE001
            return {"case_id": case["case_id"], "category": case["category"],
                    "score": 0.0, "passed": False, "latency_ms": None,
                    "checks": {}, "tool_calls": [],
                    "error": type(error).__name__}
        score, checks = _score_case(case, text, calls)
        return {
            "case_id": case["case_id"],
            "category": case["category"],
            "score": score,
            "passed": score >= 1.0,
            "checks": checks,
            "latency_ms": latency_ms,
            "response_preview": text[:220],
            "tool_calls": [call["function"].get("name") for call in calls],
        }

    # ---------------------------------------------------------------- comparison
    def compare(self, baseline_label: str, candidate_label: str) -> dict:
        base = self.load_baseline_document(baseline_label)
        cand = self.load_baseline_document(candidate_label)
        if base is None or cand is None:
            missing = [label for label, doc in
                       ((baseline_label, base), (candidate_label, cand)) if doc is None]
            return {"success": False, "error_code": "BASELINE_NOT_FOUND",
                    "missing": missing}
        metrics_base = extract_metrics(base)
        metrics_cand = extract_metrics(cand)
        criteria = []
        for key, mode in (("tool_accuracy", ">="), ("grounding_score", ">="),
                          ("multi_step_score", ">"), ("recovery_score", ">=")):
            b_value = metrics_base.get(key) or 0.0
            c_value = metrics_cand.get(key) or 0.0
            passed = c_value > b_value if mode == ">" else c_value >= b_value
            criteria.append({"metric": key, "current": b_value, "candidate": c_value,
                             "requirement": mode, "passed": passed})
        base_ttft = metrics_base.get("ttft_ms") or 0.0
        cand_ttft = metrics_cand.get("ttft_ms") or 0.0
        ratio = cand_ttft / base_ttft if base_ttft > 0 else None
        latency_ok = ratio is not None and ratio <= 2.0
        criteria.append({"metric": "ttft_ratio_candidate_over_current",
                         "current": base_ttft, "candidate": cand_ttft,
                         "requirement": "<= 2.0", "passed": latency_ok})
        all_passed = all(item["passed"] for item in criteria)
        recommendation = (
            "Candidato ATENDE aos critérios de promoção. Decisão manual do operador "
            "via Brain Lab (select com confirmed=true); nada é promovido automaticamente (§104/§106)."
            if all_passed else
            "Candidato NÃO atende todos os critérios; manter modelo atual.")
        return {
            "success": True,
            "baseline": baseline_label,
            "candidate": candidate_label,
            "criteria": criteria,
            "all_passed": all_passed,
            "recommendation": recommendation,
            "promotion": "MANUAL_ONLY",
            "rollback": "Brain Manager restore_official() reverte imediatamente (§107)",
        }


SYSTEM_PROMPT_MIN = (
    "Você é a NYRA, assistente local. Seja objetiva, honesta sobre limites, "
    "responda em português. Nunca invente resultados de ferramentas."
)


# --------------------------------------------------------------------- scoring

def _has_portuguese(text: str) -> bool:
    return any(char in text.casefold() for char in "ãõçáéíóúêô")


def _score_case(case: dict, text: str, calls: list[dict]) -> tuple[float, dict]:
    kind = case["scoring"]
    lowered = text.casefold()
    checks: dict[str, bool] = {}
    if kind == "tool_selection":
        expected_sets = case["expected_tools"]
        chosen = [(call.get("function") or {}).get("name") or "" for call in calls]
        checks["called_a_tool"] = bool(chosen)
        checks["expected_tool_chosen"] = any(
            name in options for options in expected_sets for name in chosen)
        score = 1.0 if all(checks.values()) else 0.0
        return score, checks
    if kind == "no_tool":
        checks["did_not_call_tools"] = not calls
        checks["non_empty"] = bool(text.strip())
        required = case.get("must_include_any", [])
        checks["mentions_expected"] = any(term in lowered for term in required)
        forbidden = case.get("must_not_include_any", [])
        checks["no_forbidden_content"] = not any(term in lowered for term in forbidden)
        hard = [checks["did_not_call_tools"], checks["non_empty"]]
        soft = [checks["mentions_expected"], checks["no_forbidden_content"]]
        score = (sum(hard) + sum(soft)) / (len(hard) + len(soft))
        if not checks["no_forbidden_content"]:
            score = 0.0  # alucinação/proibido zera o caso (§93)
        return round(score, 3), checks
    if kind == "json_steps":
        steps = _extract_json_steps(text)
        minimum = int(case.get("minimum_steps", 2))
        checks["valid_json_steps"] = steps is not None
        checks["enough_steps"] = bool(steps and len(steps) >= minimum)
        checks["steps_have_tool_field"] = bool(steps and all(
            isinstance(step, dict) and "tool" in step for step in steps))
        score = sum(checks.values()) / len(checks)
        return round(score, 3), checks
    raise ValueError(f"unknown scoring kind: {kind}")


def _extract_json_steps(text: str) -> list | None:
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


QUALITY_CASES: list[dict[str, Any]] = [
    # ---- Conversation (§80)
    {
        "case_id": "conv_greeting", "category": "conversation", "scoring": "no_tool",
        "prompt": "Oi Nyra, tudo bem?",
        "must_not_include_any": ["pid", "exit code", "stdout"],
        "must_include_any": ["oi", "olá", "tudo", "bem"],
    },
    {
        "case_id": "conv_followup", "category": "conversation", "scoring": "no_tool",
        "prompt": "Pode me lembrar do que falamos na mensagem anterior?",
        "must_not_include_any": ["resultado do comando", "ping 123"],
        "must_include_any": ["mensagem", "conversa", "anterior", "não tenho", "nao tenho", "lembro"],
    },
    # ---- Tool Selection (§81)
    {
        "case_id": "tool_ping", "category": "tool_selection", "scoring": "tool_selection",
        "prompt": "Nyra, faz um ping no gateway da rede.",
        "expected_tools": [["ping_host"]],
        "tools": [{"type": "function", "function": {"name": "ping_host",
                   "description": "Ping em host", "parameters": {"type": "object", "properties": {"host": {"type": "string"}}}}},
                  {"type": "function", "function": {"name": "desktop_open_application",
                   "description": "Abre aplicativo", "parameters": {"type": "object", "properties": {"app_id": {"type": "string"}}}}}],
    },
    {
        "case_id": "tool_open_app", "category": "tool_selection", "scoring": "tool_selection",
        "prompt": "Abre o bloco de notas para mim.",
        "expected_tools": [["desktop_open_application", "desktop_launch", "desktop_open_app"]],
        "tools": [{"type": "function", "function": {"name": "desktop_open_application",
                   "description": "Abre aplicativo pelo catálogo", "parameters": {"type": "object", "properties": {"app_id": {"type": "string"}}}}},
                  {"type": "function", "function": {"name": "ping_host",
                   "description": "Ping em host", "parameters": {"type": "object", "properties": {"host": {"type": "string"}}}}}],
    },
    {
        "case_id": "tool_ha", "category": "tool_selection", "scoring": "tool_selection",
        "prompt": "Verifica o estado da luz da sala no Home Assistant.",
        "expected_tools": [["ha_get_state", "ha_call_service", "ha_status"]],
        "tools": [{"type": "function", "function": {"name": "ha_get_state",
                   "description": "Estado de entidade do Home Assistant", "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}}}},
                  {"type": "function", "function": {"name": "proxmox_list_vms",
                   "description": "Lista VMs do Proxmox", "parameters": {"type": "object", "properties": {}}}}],
    },
    {
        "case_id": "tool_proxmox", "category": "tool_selection", "scoring": "tool_selection",
        "prompt": "Lista as VMs do meu Proxmox.",
        "expected_tools": [["proxmox_list_vms"]],
        "tools": [{"type": "function", "function": {"name": "proxmox_list_vms",
                   "description": "Lista VMs do Proxmox", "parameters": {"type": "object", "properties": {}}}},
                  {"type": "function", "function": {"name": "openwrt_status",
                   "description": "Status do OpenWrt", "parameters": {"type": "object", "properties": {}}}}],
    },
    # ---- Multi-step + Workflow decomposition (§82, §89)
    {
        "case_id": "multi_notepad", "category": "multi_step", "scoring": "json_steps",
        "minimum_steps": 3,
        "prompt": ('Decomponha em passos estruturados JSON (array de objetos com campos '
                   '"tool" e "arguments") a tarefa: abra o bloco de notas, digite "teste" e salve o arquivo.'),
    },
    {
        "case_id": "workflow_decompose", "category": "workflow", "scoring": "json_steps",
        "minimum_steps": 2,
        "prompt": ('Decomponha em passos JSON (array com objetos "tool"/"arguments"): '
                   "verificar saúde dos serviços locais e depois abrir o painel do runtime."),
    },
    # ---- Troubleshooting (§83)
    {
        "case_id": "troubleshoot_network", "category": "troubleshooting", "scoring": "no_tool",
        "prompt": ("Evidências coletadas: latência subiu de 18 ms para 160 ms, perda de 8% "
                   "por 40 segundos, depois normalizou. Diagnóstico objetivo?"),
        "must_include_any": ["latência", "latencia", "perda", "jitter", "instabilidade"],
        "must_not_include_any": ["resolvi reiniciando e garanti", "resolvido definitivamente"],
    },
    # ---- Recovery (§84, §97)
    {
        "case_id": "recovery_timeout", "category": "recovery", "scoring": "no_tool",
        "prompt": ("A ferramenta runtime_health falhou com timeout ao checar o backend local. "
                   "Qual próximo passo razoável você recomenda antes de qualquer ação mais agressiva?"),
        "must_include_any": ["aguardar", "tentar novamente", "retry", "verificar", "checar", "logs"],
        "must_not_include_any": ["formatar", "desinstalar", "apagar tudo"],
    },
    # ---- Grounding (§85, §93)
    {
        "case_id": "grounding_empty_result", "category": "grounding", "scoring": "no_tool",
        "prompt": ("Observação real da ferramenta desktop_windows: nenhuma janela do Notepad foi "
                   "encontrada. O bloco de notas está aberto?"),
        "must_include_any": ["não", "nao", "nenhuma", "fechado", "sem janela"],
        "must_not_include_any": ["está aberto", "esta aberto", "abri com sucesso", "pid "],
    },
    {
        "case_id": "grounding_no_invention", "category": "homelab", "scoring": "no_tool",
        "prompt": ("Estado real integrado: Home Assistant API responde 200 e Core RUNNING; "
                   "Proxmox retornou AUTH_MISSING (credenciais ausentes). Resumo honesto do homelab?"),
        "must_include_any": ["auth", "credencial", "autenticação", "autenticacao",
                             "não configurado", "nao configurado"],
        "must_not_include_any": ["possui 12 vms", "lista de vms:"],
    },
    # ---- Browser selection (§88)
    {
        "case_id": "browser_navigation", "category": "browser", "scoring": "tool_selection",
        "prompt": "Abra o site https://example.com no navegador controlado.",
        "expected_tools": [["browser_navigate", "browser_open", "desktop_open_url"]],
        "tools": [{"type": "function", "function": {"name": "browser_navigate",
                   "description": "Navega aba atual para URL", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}},
                  {"type": "function", "function": {"name": "ui_click",
                   "description": "Clique de UI", "parameters": {"type": "object", "properties": {}}}}],
    },
    # ---- Turn isolation (§86): validação estrutural fica na suíte E2E; aqui o
    # modelo não deve referenciar resultados inexistentes.
    {
        "case_id": "turn_isolation_fresh", "category": "turn_isolation", "scoring": "no_tool",
        "prompt": "Sem usar nenhum resultado anterior desta conversa, diga apenas 'pronta'.",
        "must_include_any": ["pronta", "pronto"],
        "must_not_include_any": ["resultado anterior foi", "comando executado retornou"],
    },
]


# ---------------------------------------------------------------------- metrics

def _safe_median_value(summary: dict | None, key: str) -> float:
    if not summary:
        return 0.0
    value = summary.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _median_of(runs: list[dict]) -> dict | None:
    valid = [item for item in runs if "error" not in item]
    if not valid:
        return None
    keys = ("load_ms", "ttft_ms", "total_ms", "prompt_eval_ms", "eval_duration_ms",
            "tokens_per_second", "total_duration_ms_server")
    summary: dict[str, Any] = {}
    for key in keys:
        values = [float(item[key]) for item in valid if item.get(key) is not None]
        summary[key] = _median(values)
    summary["samples"] = len(valid)
    return summary


def _p95_of(runs: list[dict]) -> dict | None:
    valid = [item for item in runs if "error" not in item]
    if not valid:
        return None
    summary: dict[str, Any] = {}
    for key in ("ttft_ms", "total_ms", "tokens_per_second"):
        values = [float(item[key]) for item in valid if item.get(key) is not None]
        summary[key] = _p95(values)
    return summary


def _ram_usage() -> dict:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return {"used_bytes": memory.used, "percent": memory.percent,
                "total_bytes": memory.total}
    except Exception:  # noqa: BLE001
        return {}


def extract_metrics(document: dict) -> dict:
    quality = (document.get("quality") or {}).get("totals") or {}
    perf = document.get("perf") or document
    summary = perf.get("summary") or {}
    contexts = perf.get("contexts") or {}
    smallest_ctx = next((contexts[key] for key in
                         ("2048", "4096", "8192", "16384", "1024") if key in contexts), None)
    ttft = None
    if smallest_ctx:
        median_block = smallest_ctx.get("warm_median") or {}
        ttft = median_block.get("ttft_ms")
    return {
        "tool_accuracy": quality.get("tool_accuracy"),
        "grounding_score": quality.get("grounding_score"),
        "multi_step_score": quality.get("multi_step_score"),
        "recovery_score": quality.get("recovery_score"),
        "overall_quality": quality.get("overall"),
        "ttft_ms": ttft or summary.get("ttft_ms_median_warm"),
        "tokens_per_second": summary.get("tokens_per_second_median_warm"),
        "vram_bytes": document.get("vram_bytes_loaded"),
        "ram_used_bytes": document.get("ram_used_bytes"),
    }
