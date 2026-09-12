import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from .core import (
        AGY_BIN,
        is_profile_logged_in,
        list_all_profiles,
        list_pool_profiles,
        is_profile_reserved,
        get_profile_home,
        validate_profile_name
    )
    from .quota import get_cached_quotas
except Exception:
    from core import (
        AGY_BIN,
        is_profile_logged_in,
        list_all_profiles,
        list_pool_profiles,
        is_profile_reserved,
        get_profile_home,
        validate_profile_name
    )
    from quota import get_cached_quotas

logger = logging.getLogger("agy-pool-router")

DEFAULT_MODEL = "gemini-3.8-flash-low"
MIN_QUOTA_THRESHOLD = 10.0  # Procent ponizej ktorego konto jest pomijane

ALLOWED_MODELS = [
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-3.7-flash-low",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-high",
    "gemini-3.6-flash-low",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-high",
    "gemini-3.1-pro-low",
    "gemini-3.1-pro-medium",
    "gemini-3.1-pro-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium"
]

def normalize_model(model_name: Optional[str]) -> str:
    if not model_name or not model_name.strip():
        return DEFAULT_MODEL
    cleaned = model_name.strip()
    if cleaned.startswith("agy-") or cleaned.startswith("agy:"):
        cleaned = cleaned[4:]
    if cleaned in ALLOWED_MODELS:
        return cleaned
    # Map common aliases
    if "3.8" in cleaned or "flash-low" in cleaned:
        return "gemini-3.8-flash-low"
    if "flash" in cleaned:
        return "gemini-3.8-flash-low"
    if "3.1" in cleaned or "pro" in cleaned:
        return "gemini-3.1-pro-high"
    if "claude" in cleaned or "sonnet" in cleaned or "3.5" in cleaned:
        return "claude-sonnet-4-6"
    return DEFAULT_MODEL

def get_model_family(model: str) -> str:
    norm = normalize_model(model)
    if "claude" in norm or "gpt" in norm:
        return "claude"
    return "gemini"

class AccountPoolRouter:
    def __init__(self):
        self._round_robin_idx: int = 0
        self._temp_cooldowns: Dict[str, float] = {}  # account -> unix_timestamp_until
        self._session_account_map: Dict[str, str] = {}  # conv_uuid -> account
        self._stats: Dict[str, Dict[str, int]] = {}
        self._lock = asyncio.Lock()

    def get_healthy_accounts(self, family: str = "gemini", min_quota: float = MIN_QUOTA_THRESHOLD) -> List[Tuple[str, int]]:
        """
        Zwraca liste kont (profil, remaining_pct), które sa zalogowane,
        posiadaja limit > min_quota i nie sa w chwilowym cooldownie.
        Sciśle wyklucza profil 'klajner' (zarezerwowany dla Pawła) oraz profile systemowe.
        """
        now = time.time()
        quotas = get_cached_quotas()
        pool_profs = list_pool_profiles()
        healthy = []

        for p in pool_profs:
            if is_profile_reserved(p) or not is_profile_logged_in(p):
                continue

            # Sprawdz czy konto nie jest w tymczasowym backoffie po bledzie
            if p in self._temp_cooldowns and self._temp_cooldowns[p] > now:
                continue

            q = quotas.get(p, {})
            if family == "claude":
                pct = q.get("claude_effective_pct")
                status = q.get("claude_status")
            else:
                pct = q.get("gemini_effective_pct")
                status = q.get("gemini_status")

            if pct is not None and pct > min_quota and status == "Available":
                healthy.append((p, pct))

        # Sortuj stabilnie wg nazwy profilu dla przewidywalnego round-robin
        healthy.sort(key=lambda x: x[0])
        return healthy

    def get_best_profile(self, family: str = "gemini") -> Tuple[str, Dict[str, Any]]:
        """
        Zwraca profil z puli ogolnej (account-01..account-12) z NAJWIĘKSZĄ ilością dostępnych zasobów
        (najwyższy efektywny procent limitu, najdłuższy czas do wyczerpania).
        Sciśle wyklucza profil Klajner (zarezerwowany dla Pawła) oraz profile systemowe.
        """
        quotas = get_cached_quotas()
        candidates = []
        now = time.time()
        pool_profs = list_pool_profiles()

        for p in pool_profs:
            if is_profile_reserved(p) or not is_profile_logged_in(p):
                continue
            if p in self._temp_cooldowns and self._temp_cooldowns[p] > now:
                continue

            q = quotas.get(p, {})
            g_pct = q.get("gemini_effective_pct")
            g_stat = q.get("gemini_status")
            c_pct = q.get("claude_effective_pct")
            c_stat = q.get("claude_status")
            five_h = q.get("gemini_5h_pct") or 0

            # Ocena zasobow
            if family == "claude":
                primary = c_pct if (c_pct is not None and c_stat == "Available") else -1
                secondary = g_pct if (g_pct is not None and g_stat == "Available") else -1
            else:
                primary = g_pct if (g_pct is not None and g_stat == "Available") else -1
                secondary = c_pct if (c_pct is not None and c_stat == "Available") else -1

            candidates.append({
                "profile": p,
                "primary": primary,
                "secondary": secondary,
                "five_h": five_h,
                "quota": q
            })

        if not candidates:
            # Awaryjny fallback na dowolne zalogowane konto z puli ogolnej
            for p in pool_profs:
                if is_profile_logged_in(p):
                    q = quotas.get(p, {})
                    candidates.append({
                        "profile": p,
                        "primary": q.get(f"{family}_effective_pct") or 0,
                        "secondary": 0,
                        "five_h": 0,
                        "quota": q
                    })

        if not candidates:
            raise RuntimeError("Brak jakichkolwiek dostępnych kont w puli AGY (z wyłączeniem profilu Klajner)!")

        # Sortuj: 1. Najwyzszy limit glowny, 2. Najwyzszy limit dodatkowy, 3. Okno 5h, 4. Nazwa
        candidates.sort(key=lambda x: (x["primary"], x["secondary"], x["five_h"]), reverse=True)
        best = candidates[0]
        return best["profile"], best["quota"]

    async def select_account(self, model: str, conversation_uuid: Optional[str] = None) -> Tuple[str, str, int]:
        """
        Wybiera sprawne konto z kolektora AI.
        Jesli dla zadanej rodziny brak kont, probuje automatycznego failovera.
        Zwraca (selected_profile, effective_model, remaining_pct).
        """
        async with self._lock:
            family = get_model_family(model)
            effective_model = normalize_model(model)

            healthy = self.get_healthy_accounts(family)

            # Failover na Claude jesli Gemini wyczerpane na wszystkich kontach
            if not healthy and family == "gemini":
                claude_healthy = self.get_healthy_accounts("claude")
                if claude_healthy:
                    logger.warning("[POOL-ROUTER] Wszystkie konta Gemini wyczerpane! Automatyczny failover na Claude.")
                    family = "claude"
                    effective_model = "claude-sonnet-4-6"
                    healthy = claude_healthy

            # Failover na Gemini jesli Claude wyczerpane
            elif not healthy and family == "claude":
                gemini_healthy = self.get_healthy_accounts("gemini")
                if gemini_healthy:
                    logger.warning("[POOL-ROUTER] Wszystkie konta Claude wyczerpane! Automatyczny failover na Gemini.")
                    family = "gemini"
                    effective_model = "gemini-3.7-flash-high"
                    healthy = gemini_healthy

            if not healthy:
                # W ostatecznosci pobierz jakiekolwiek zalogowane konto z puli z najwiekszym limitem
                quotas = get_cached_quotas()
                candidates = []
                for p in list_pool_profiles():
                    if is_profile_logged_in(p):
                        q = quotas.get(p, {})
                        pct = q.get(f"{family}_effective_pct") or 0
                        candidates.append((p, pct))
                if candidates:
                    candidates.sort(key=lambda x: x[1], reverse=True)
                    best_p, best_pct = candidates[0]
                    logger.critical(f"[POOL-ROUTER] Brak kont z limitem >{MIN_QUOTA_THRESHOLD}%. Wybieram najlepsze dostepne z puli ogolnej: {best_p} ({best_pct}%)")
                    return best_p, effective_model, best_pct
                raise Exception("Brak jakichkolwiek zalogowanych kont w puli ogólnej /srv/projects/agy!")

            # Logika krążenia (Round-Robin / Session Affinity)
            # Jeśli sesja ma przypisane konto i to konto nadal jest sprawne -> utrzymaj je
            if conversation_uuid and conversation_uuid in self._session_account_map:
                prev_acc = self._session_account_map[conversation_uuid]
                matching = [item for item in healthy if item[0] == prev_acc]
                if matching:
                    return matching[0][0], effective_model, matching[0][1]
                else:
                    logger.info(f"[POOL-ROUTER] Poprzednie konto sesji {conversation_uuid} ({prev_acc}) ma limit <=10%. Przelaczam na nastepne wolne konto.")

            # Wybierz nastepne konto w rotacji Round-Robin
            idx = self._round_robin_idx % len(healthy)
            self._round_robin_idx += 1
            selected_profile, pct = healthy[idx]

            if conversation_uuid:
                self._session_account_map[conversation_uuid] = selected_profile

            # Inicjalizacja statystyk
            if selected_profile not in self._stats:
                self._stats[selected_profile] = {"requests": 0, "success": 0, "errors": 0}
            self._stats[selected_profile]["requests"] += 1

            return selected_profile, effective_model, pct

    def mark_account_cooldown(self, profile: str, seconds: int = 300):
        """Oznacza konto tymczasowo wstrzymanym (np. po 429)"""
        self._temp_cooldowns[profile] = time.time() + seconds
        logger.warning(f"[POOL-ROUTER] Konto {profile} oznaczone cooldownem na {seconds}s.")

    async def execute_prompt(
        self,
        prompt: str,
        model: Optional[str] = None,
        conversation_uuid: Optional[str] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """
        Wykonuje prompt w bezpiecznym podprocesie AGY,
        rotujac po sprawnych kontach w razie bledow.
        """
        req_model = model or DEFAULT_MODEL
        last_error = None

        for attempt in range(max_retries):
            profile, eff_model, quota_pct = await self.select_account(req_model, conversation_uuid)
            home_dir = get_profile_home(profile)
            
            logger.info(
                f"[POOL-ROUTER] [Prob {attempt+1}/{max_retries}] "
                f"Konto: {profile} ({quota_pct}% limitu) -> Model: {eff_model}"
            )

            cmd = [
                AGY_BIN,
                "-p", prompt,
                "--model", eff_model,
                "--output-format", "json",
                "--disable-slash-commands",
                "--dangerously-skip-permissions"
            ]
            if conversation_uuid:
                cmd.extend(["--conversation", conversation_uuid])

            env = os.environ.copy()
            env["HOME"] = home_dir
            env["PATH"] = f"/home/kacper/.local/bin:{env.get('PATH', '')}"

            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35.0)
                duration = time.time() - t0

                if proc.returncode != 0:
                    err_text = stderr.decode("utf-8", errors="ignore").strip() or stdout.decode("utf-8", errors="ignore").strip()
                    logger.warning(f"[POOL-ROUTER] Konto {profile} zakonczylo sie kodem {proc.returncode}: {err_text}")
                    self.mark_account_cooldown(profile, seconds=300)
                    if profile in self._stats:
                        self._stats[profile]["errors"] += 1
                    last_error = err_text
                    # Rotuj natychmiast na inne konto
                    continue

                out_raw = stdout.decode("utf-8", errors="ignore").strip()
                try:
                    data = json.loads(out_raw)
                    response_text = data.get("response", "").strip()
                    resp_uuid = data.get("conversation_id") or conversation_uuid
                    usage = data.get("usage", {})

                    if profile in self._stats:
                        self._stats[profile]["success"] += 1

                    return {
                        "status": "SUCCESS",
                        "response": response_text,
                        "account_used": profile,
                        "model_used": eff_model,
                        "quota_remaining_pct": quota_pct,
                        "conversation_id": resp_uuid,
                        "duration_seconds": duration,
                        "usage": usage
                    }
                except Exception as parse_err:
                    logger.warning(f"[POOL-ROUTER] Nie udalo sie sparsowac wyjscia JSON z {profile}: {parse_err}. Zwracam surowy tekst.")
                    if profile in self._stats:
                        self._stats[profile]["success"] += 1
                    return {
                        "status": "SUCCESS",
                        "response": out_raw,
                        "account_used": profile,
                        "model_used": eff_model,
                        "quota_remaining_pct": quota_pct,
                        "conversation_id": conversation_uuid or "",
                        "duration_seconds": duration,
                        "usage": {}
                    }

            except asyncio.TimeoutError:
                logger.warning(f"[POOL-ROUTER] Timeout 35s dla konta {profile}. Oznaczam wstrzymanie.")
                self.mark_account_cooldown(profile, seconds=180)
                if profile in self._stats:
                    self._stats[profile]["errors"] += 1
                last_error = "Timeout 35s"
                continue
            except Exception as exc:
                logger.error(f"[POOL-ROUTER] Wyjatek podczas wykonania na koncie {profile}: {exc}")
                self.mark_account_cooldown(profile, seconds=180)
                last_error = str(exc)
                continue

        raise Exception(f"Wszystkie proby wykonania zapytania w puli AGY zawiodly. Ostatni blad: {last_error}")

    def get_pool_status(self) -> Dict[str, Any]:
        """Zwraca pelny podglad stanu puli kont, limitow i statystyk."""
        quotas = get_cached_quotas()
        gemini_healthy = self.get_healthy_accounts("gemini")
        claude_healthy = self.get_healthy_accounts("claude")

        accounts_detail = []
        for p in list_all_profiles():
            q = quotas.get(p, {})
            is_cool = p in self._temp_cooldowns and self._temp_cooldowns[p] > time.time()
            accounts_detail.append({
                "profile": p,
                "logged_in": is_profile_logged_in(p),
                "in_cooldown": is_cool,
                "cooldown_remaining_sec": max(0, int(self._temp_cooldowns[p] - time.time())) if is_cool else 0,
                "gemini_effective_pct": q.get("gemini_effective_pct"),
                "gemini_status": q.get("gemini_status"),
                "gemini_wait": q.get("gemini_wait_human"),
                "claude_effective_pct": q.get("claude_effective_pct"),
                "claude_status": q.get("claude_status"),
                "claude_wait": q.get("claude_wait_human"),
                "stats": self._stats.get(p, {"requests": 0, "success": 0, "errors": 0})
            })

        return {
            "total_accounts": len(accounts_detail),
            "healthy_gemini_accounts": len(gemini_healthy),
            "healthy_claude_accounts": len(claude_healthy),
            "gemini_pool": [x[0] for x in gemini_healthy],
            "claude_pool": [x[0] for x in claude_healthy],
            "active_sessions_tracked": len(self._session_account_map),
            "accounts": accounts_detail
        }

# Global singleton
pool_router = AccountPoolRouter()
