"""
Rate Limiter Service
Gestisce rate limiting per chiamate API (Mistral, Document Intelligence, OpenAI)
"""
import asyncio
import time
import logging
from collections import deque

from config import (
    MISTRAL_RATE_LIMIT_GLOBAL,
    MAX_CONTAINER_INSTANCES,
    RATE_LIMIT_RETRY_BASE_DELAY,
    RATE_LIMIT_RETRY_MAX_DELAY,
    RATE_LIMIT_RETRY_MAX_ATTEMPTS
)

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """Eccezione per rate limit (HTTP 429)"""
    pass


class RateLimiter:
    """
    Rate limiter per istanza singola.
    Il limite per istanza è calcolato come limite_globale / num_istanze
    """

    def __init__(
        self,
        service_name: str,
        global_limit_per_minute: int,
        num_instances: int = MAX_CONTAINER_INSTANCES
    ):
        """
        Inizializza rate limiter

        Args:
            service_name: Nome del servizio (per logging)
            global_limit_per_minute: Limite globale req/min
            num_instances: Numero max istanze Container App
        """
        self.service_name = service_name
        self.global_limit = global_limit_per_minute
        self.instance_limit = max(1, global_limit_per_minute // num_instances)
        self.call_times: deque[float] = deque()
        self._lock = asyncio.Lock()

        logger.info(
            f"RateLimiter {service_name}: {self.instance_limit} req/min per istanza "
            f"(globale: {global_limit_per_minute}, istanze: {num_instances})"
        )

    async def acquire(self):
        """
        Attende se necessario per rispettare il rate limit.
        Chiamare prima di ogni richiesta API.

        IMPORTANTE: lo sleep avviene FUORI dal lock per non bloccare
        le altre coroutine che aspettano di acquisire il rate limiter.
        """
        while True:
            sleep_time = 0.0

            async with self._lock:
                now = time.time()

                # Rimuovi chiamate più vecchie di 60 secondi
                while self.call_times and self.call_times[0] < now - 60:
                    self.call_times.popleft()

                # Se sotto il limite, registra e procedi
                if len(self.call_times) < self.instance_limit:
                    self.call_times.append(time.time())
                    return

                # Calcola quanto aspettare (fuori dal lock)
                oldest_call = self.call_times[0]
                sleep_time = 60 - (now - oldest_call) + 0.1

            # Sleep FUORI dal lock - le altre coroutine possono procedere
            if sleep_time > 0:
                logger.debug(
                    f"{self.service_name} rate limit raggiunto "
                    f"({self.instance_limit}/{self.instance_limit}), "
                    f"attendo {sleep_time:.1f}s"
                )
                await asyncio.sleep(sleep_time)
            # Dopo lo sleep, ri-controlla (loop)

    def get_current_usage(self) -> tuple[int, int]:
        """
        Ritorna utilizzo corrente (calls_in_window, limit)
        """
        now = time.time()
        # Conta solo chiamate negli ultimi 60 secondi
        recent_calls = sum(1 for t in self.call_times if t > now - 60)
        return recent_calls, self.instance_limit

    async def wait_if_needed(self, min_delay: float = 0):
        """
        Attende un delay minimo se specificato, utile per backoff
        """
        if min_delay > 0:
            await asyncio.sleep(min_delay)
        await self.acquire()


# ============= ISTANZE RATE LIMITER GLOBALI =============

# Mistral Document AI - 600 req/min TOTALI (BOTTLENECK principale)
mistral_limiter = RateLimiter(
    service_name="Mistral",
    global_limit_per_minute=MISTRAL_RATE_LIMIT_GLOBAL,
    num_instances=MAX_CONTAINER_INSTANCES
)

# Claude Sonnet 4.5 (Azure AI Foundry) - 4M token/min, 4000 req/min
claude_limiter = RateLimiter(
    service_name="Claude",
    global_limit_per_minute=4000,
    num_instances=MAX_CONTAINER_INSTANCES
)


async def with_retry(
    func,
    max_attempts: int = RATE_LIMIT_RETRY_MAX_ATTEMPTS,
    base_delay: float = RATE_LIMIT_RETRY_BASE_DELAY,
    max_delay: float = RATE_LIMIT_RETRY_MAX_DELAY
):
    """
    Esegue una funzione async con retry e exponential backoff

    Args:
        func: Funzione async da eseguire
        max_attempts: Numero massimo tentativi
        base_delay: Delay base in secondi
        max_delay: Delay massimo in secondi

    Returns:
        Risultato della funzione

    Raises:
        L'ultima eccezione se tutti i tentativi falliscono
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except RateLimitError as e:
            last_exception = e
            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.warning(
                    f"Rate limit, retry {attempt}/{max_attempts} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"Rate limit, tutti i {max_attempts} tentativi falliti")
        except Exception as e:
            last_exception = e
            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.warning(
                    f"Errore: {e}, retry {attempt}/{max_attempts} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"Errore, tutti i {max_attempts} tentativi falliti: {e}")

    raise last_exception

