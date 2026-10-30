"""
Database Service
Gestisce connessioni e operazioni MySQL
"""
import asyncio
import logging
import random
import time
from contextlib import contextmanager
from typing import Optional

import mysql.connector

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
from services.claude_service import ClaudeService

logger = logging.getLogger(__name__)

# Cache in-memory per tabelle di riferimento (dipendenti, qualifica, classe_cespiti).
# Queste tabelle sono piccole e stabili durante l'import: caricarle una volta
# elimina i full table scan ripetuti.
_ref_cache: dict[str, tuple[float, list[dict]]] = {}
REF_CACHE_TTL = 300  # 5 minuti

# Cache lookup individuali: (tipo, input_normalizzato) -> (timestamp, risultato)
# Evita query SQL ripetute per lo stesso input (es. stesso dipendente in piu' file).
# asyncio = single-threaded, nessun lock necessario.
_lookup_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}

# Codici errore MySQL transitori (non risolvibili con correzione SQL)
_TRANSIENT_MYSQL_CODES = {1040, 1205, 1213, 2006, 2013, 2055}
_TRANSIENT_STRINGS = (
    'too many connections',
    'mysql connection not available',
    'server has gone away',
    'lost connection',
    'lock wait timeout',
    'deadlock found',
    'connection was killed',
)


def _is_transient_error(exc: Exception) -> bool:
    """
    True se l'errore e' transiente (lock timeout, deadlock, connessione persa,
    too many connections). Gestiti con retry diretto, NON con correzione Claude.
    """
    # mysql.connector.Error ha errno
    if hasattr(exc, 'errno') and exc.errno in _TRANSIENT_MYSQL_CODES:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_STRINGS)


_logged_first_conn = False


def _create_connection(autocommit=True) -> mysql.connector.MySQLConnection:
    """
    Crea una connessione MySQL diretta (no pool).
    Retry con backoff per 'Too many connections'.
    """
    global _logged_first_conn
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            conn = mysql.connector.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                charset='utf8mb4',
                collation='utf8mb4_unicode_ci',
                autocommit=autocommit
            )
            if not _logged_first_conn:
                logger.info(f"Connessione MySQL: {DB_HOST}:{DB_PORT}/{DB_NAME} (autocommit={autocommit})")
                _logged_first_conn = True
            return conn
        except mysql.connector.errors.OperationalError as e:
            if 'Too many connections' in str(e) and attempt < max_attempts:
                wait = (2 ** attempt) + random.uniform(0.5, 2.0)
                logger.warning(
                    f"Too many connections (tentativo {attempt}/{max_attempts}). "
                    f"Retry tra {wait:.1f}s"
                )
                time.sleep(wait)
                continue
            raise


def invalidate_ref_cache():
    """Svuota la cache delle tabelle di riferimento e dei lookup."""
    _ref_cache.clear()
    _lookup_cache.clear()
    logger.info("Cache tabelle di riferimento e lookup invalidata")


@contextmanager
def _get_conn(dictionary=False):
    """
    Context manager per connessione diretta + cursore.
    Apre connessione, la chiude alla fine.
    """
    conn = _create_connection()
    cursor = conn.cursor(dictionary=dictionary)
    try:
        yield conn, cursor
    finally:
        cursor.close()
        conn.close()


class DBService:
    """
    Servizio per operazioni database MySQL
    """

    def __init__(self):
        self.claude_service = ClaudeService()

    def execute_query(self, query: str, params: tuple = None) -> tuple[bool, int | None]:
        """
        Esegue una query SQL (autocommit=True, ogni statement fa commit automatico).

        Returns:
            (success, lastrowid) - lastrowid e' l'ID generato per INSERT
        """
        with _get_conn() as (conn, cursor):
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            lastrowid = cursor.lastrowid
            logger.debug(f"Query eseguita: {cursor.rowcount} righe affected, lastrowid={lastrowid}")
            return True, lastrowid

    def fetch_one(self, query: str, params: tuple = None) -> dict | None:
        """Esegue query e ritorna una riga come dict"""
        with _get_conn(dictionary=True) as (conn, cursor):
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            return cursor.fetchone()

    def fetch_all(self, query: str, params: tuple = None) -> list[dict]:
        """Esegue query e ritorna tutte le righe come lista di dict"""
        with _get_conn(dictionary=True) as (conn, cursor):
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            return cursor.fetchall()

    async def _execute_write_with_retry(self, query: str, max_attempts: int = 4) -> tuple[bool, int | None]:
        """
        Esegue una query write con retry per errori transitori
        (lock timeout, deadlock, connessione persa).
        Exponential backoff: 1s, 2s, 4s + jitter.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                return await asyncio.to_thread(self.execute_query, query)
            except Exception as e:
                if _is_transient_error(e) and attempt < max_attempts:
                    wait = (2 ** (attempt - 1)) + random.uniform(0.1, 1.0)
                    logger.warning(
                        f"Errore transiente DB (tentativo {attempt}/{max_attempts}): "
                        f"{str(e)[:120]}. Retry tra {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue

                raise

    async def execute_insert_with_retry(
        self,
        query: str,
        json_data: dict,
        doc_type: str,
        max_retries: int = 2
    ) -> tuple[bool, str | None, int | None]:
        """
        Esegue INSERT con retry:
        - Errori transitori (lock timeout, deadlock, connessione): retry diretto (NO Claude)
        - Errori SQL genuini (sintassi, constraint): correzione Claude

        Args:
            query: Query SQL da eseguire
            json_data: Dati estratti (per correzione Claude)
            doc_type: Tipo documento
            max_retries: Numero massimo retry con Claude per errori SQL

        Returns:
            (success: bool, error_message: str | None, lastrowid: int | None)
        """
        last_error = None
        current_query = query

        for attempt in range(max_retries):
            try:
                success, lastrowid = await self._execute_write_with_retry(current_query)
                logger.info(f"INSERT riuscita (tentativo {attempt + 1}), id={lastrowid}")
                return (True, None, lastrowid)

            except Exception as e:
                last_error = str(e)
                logger.error(f"INSERT fallita (tentativo {attempt + 1}): {last_error}")

                # Errore transiente: _execute_write_with_retry ha gia' esaurito i retry interni.
                # Non ha senso chiamare Claude per correggerlo.
                if _is_transient_error(e):
                    logger.warning(
                        f"Errore transiente esaurito retry interni, skip correzione Claude"
                    )
                    break

                # Errore SQL genuino: chiedi correzione a Claude
                if attempt < max_retries - 1:
                    logger.info("Richiedo correzione a Claude...")

                    corrected = await self.claude_service.fix_sql_query(
                        original_query=current_query,
                        error_message=last_error,
                        json_data=json_data,
                        doc_type=doc_type
                    )

                    if corrected:
                        current_query = corrected
                        logger.info("Query corretta, ritento...")
                    else:
                        logger.error("Claude non e' riuscito a correggere la query")
                        break

        # Fallimento definitivo
        return (False, last_error, None)

    async def execute_detail_insert(self, query: str) -> tuple[bool, int | None]:
        """
        Esegue INSERT per righe dettaglio con retry automatico.
        Wrapper async attorno a _execute_write_with_retry.
        """
        return await self._execute_write_with_retry(query)

    async def execute_batch_insert(
        self,
        queries: list[str],
        max_attempts: int = 4
    ) -> tuple[int, list[str]]:
        """
        Esegue N INSERT in una singola transazione MySQL.
        1 lock acquisition invece di N → riduce drasticamente la contesa.

        Se un singolo INSERT fallisce, continua con i successivi (partial success).
        Se la transazione intera fallisce per errore transiente, retry con backoff.

        Args:
            queries: Lista di query INSERT da eseguire
            max_attempts: Numero massimo tentativi per la transazione

        Returns:
            (success_count, errors) - numero INSERT riuscite e lista errori
        """
        if not queries:
            return 0, []

        def _run_batch(batch_queries: list[str]) -> tuple[int, list[str]]:
            """Esegue il batch in una singola transazione (thread sync)."""
            conn = _create_connection(autocommit=False)
            cursor = conn.cursor()
            success = 0
            errors = []
            try:
                conn.start_transaction()

                for idx, q in enumerate(batch_queries):
                    try:
                        cursor.execute(q)
                        success += 1
                    except Exception as e:
                        errors.append(f"Query {idx + 1}: {str(e)[:200]}")
                        logger.warning(f"Batch INSERT query {idx + 1} fallita: {str(e)[:120]}")

                conn.commit()
                return success, errors
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                cursor.close()
                conn.close()

        for attempt in range(1, max_attempts + 1):
            try:
                success_count, errors = await asyncio.to_thread(_run_batch, queries)
                logger.info(
                    f"Batch transaction completata: {success_count}/{len(queries)} "
                    f"INSERT riuscite"
                )
                return success_count, errors
            except Exception as e:
                if _is_transient_error(e) and attempt < max_attempts:
                    wait = (2 ** (attempt - 1)) + random.uniform(0.1, 1.0)
                    logger.warning(
                        f"Batch transaction fallita per errore transiente "
                        f"(tentativo {attempt}/{max_attempts}): {str(e)[:120]}. "
                        f"Retry tra {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue

                logger.error(f"Batch transaction fallita definitivamente: {e}")
                return 0, [f"Transaction error: {str(e)[:200]}"]

        return 0, ["Max tentativi esauriti"]

    def _get_cached_ref_table(self, cache_key: str, query: str) -> list[dict]:
        """
        Ritorna dati da cache se validi, altrimenti esegue query e aggiorna cache.
        TTL di 5 minuti (le tabelle di riferimento non cambiano durante import).
        """
        now = time.time()

        if cache_key in _ref_cache:
            ts, data = _ref_cache[cache_key]
            if now - ts < REF_CACHE_TTL:
                return data

        # Cache miss o scaduta
        data = self.fetch_all(query)
        logger.info(f"Cache miss '{cache_key}': caricati {len(data)} record")

        _ref_cache[cache_key] = (now, data)

        return data

    def _get_cached_lookup(self, lookup_type: str, key: str) -> tuple[bool, dict | None]:
        """
        Controlla la cache lookup individuali.
        Returns: (hit, risultato). hit=True anche se risultato e' None (lookup negativo cachato).
        """
        now = time.time()
        cache_key = (lookup_type, key)

        if cache_key in _lookup_cache:
            ts, result = _lookup_cache[cache_key]
            if now - ts < REF_CACHE_TTL:
                logger.debug(f"Lookup cache hit: {lookup_type}:{key}")
                return True, result

        return False, None

    def _set_cached_lookup(self, lookup_type: str, key: str, result: dict | None):
        """Salva risultato in cache lookup (anche None per lookup negativi)."""
        _lookup_cache[(lookup_type, key)] = (time.time(), result)

    def get_table_schema(self, table_name: str) -> list[dict]:
        """
        Recupera lo schema di una tabella con commenti colonne

        Args:
            table_name: Nome tabella

        Returns:
            Lista di dict con column_name, column_type, column_comment
        """
        query = """
            SELECT
                column_name,
                column_type,
                column_comment,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """

        return self.fetch_all(query, (DB_NAME, table_name))

    def get_sal_id(self, sal_number: int) -> int | None:
        """
        Recupera l'id_sal dato il numero SAL

        Args:
            sal_number: Numero SAL

        Returns:
            id_sal o None se non trovato
        """
        result = self.fetch_one(
            "SELECT id_sal FROM sal WHERE numero_sal = %s",
            (sal_number,)
        )
        return result["id_sal"] if result else None

    def find_dipendente_by_name(self, nome_cognome: str) -> dict | None:
        """
        Cerca un dipendente per nome con matching multi-livello:
        1. Cache lookup
        2. Match esatto (COGNOME NOME o NOME COGNOME)
        3. Fuzzy matching con normalizzazione (soglia 75%)
        """
        if not nome_cognome:
            return None

        # Normalizza input
        nome_norm = self._normalize_name(nome_cognome)

        # Check cache lookup
        hit, cached = self._get_cached_lookup("dipendente", nome_norm)
        if hit:
            return cached

        # LIVELLO 1: Match esatto
        query_exact = """
            SELECT cod_dipendente, nome, cognome
            FROM dipendenti
            WHERE CONCAT(UPPER(cognome), ' ', UPPER(nome)) = UPPER(%s)
               OR CONCAT(UPPER(nome), ' ', UPPER(cognome)) = UPPER(%s)
            LIMIT 1
        """
        result = self.fetch_one(query_exact, (nome_cognome, nome_cognome))
        if result:
            logger.info(f"Match esatto: '{nome_cognome}' -> {result['cognome']} {result['nome']} (cod: {result['cod_dipendente']})")
            self._set_cached_lookup("dipendente", nome_norm, result)
            return result

        # LIVELLO 2: Fuzzy matching (da cache, nessun full table scan)
        dipendenti = self._get_cached_ref_table(
            "dipendenti", "SELECT cod_dipendente, nome, cognome FROM dipendenti"
        )

        best_match = None
        best_score = 0.0

        for dip in dipendenti:
            nome_db = self._normalize_name(f"{dip['cognome']} {dip['nome']}")
            nome_db_inv = self._normalize_name(f"{dip['nome']} {dip['cognome']}")

            score1 = self._calculate_similarity(nome_norm, nome_db)
            score2 = self._calculate_similarity(nome_norm, nome_db_inv)
            score = max(score1, score2)

            if score > best_score:
                best_score = score
                best_match = dip

        if best_match and best_score >= 0.75:
            logger.info(f"Fuzzy match: '{nome_cognome}' -> {best_match['cognome']} {best_match['nome']} (cod: {best_match['cod_dipendente']}, score: {best_score:.2f})")
            self._set_cached_lookup("dipendente", nome_norm, best_match)
            return best_match

        logger.warning(f"No match for: {nome_cognome} (best score: {best_score:.2f})")
        self._set_cached_lookup("dipendente", nome_norm, None)
        return None

    def _normalize_name(self, text: str) -> str:
        """Normalizza nome per matching"""
        if not text:
            return ""
        text = text.upper().strip()
        # Rimuovi apostrofi
        text = text.replace("'", "").replace("\u2019", "").replace("`", "")
        # Normalizza spazi
        return " ".join(text.split())

    def _calculate_similarity(self, s1: str, s2: str) -> float:
        """Calcola similarita' tra due stringhe"""
        from difflib import SequenceMatcher
        return SequenceMatcher(None, s1, s2).ratio()

    def find_qualifica_by_description(self, descrizione: str) -> dict | None:
        """
        Cerca una qualifica per descrizione con matching multi-livello.

        Args:
            descrizione: Descrizione della qualifica (es. "DIRIGENTE", "QUADRO", "A1")

        Returns:
            Dict con cod_qualifica e des_qualifica, o None
        """
        if not descrizione:
            return None

        descrizione_norm = self._normalize_name(descrizione)

        # Check cache lookup
        hit, cached = self._get_cached_lookup("qualifica", descrizione_norm)
        if hit:
            return cached

        # Alias comuni (valori che Claude potrebbe estrarre -> valore corretto)
        alias_map = {
            "A": "A1",
            "DIR": "DIRIGENTE",
            "DIRIG": "DIRIGENTE",
            "QUAD": "QUADRO",
            "IMP": "IMPIEGATO",
            "OP": "OPERAIO",
        }

        # Espandi alias se presente
        alias_expanded = alias_map.get(descrizione_norm, descrizione_norm)

        # LIVELLO 1: Match esatto (case-insensitive)
        query_exact = """
            SELECT cod_qualifica, des_qualifica
            FROM qualifica
            WHERE UPPER(des_qualifica) = UPPER(%s)
               OR UPPER(des_qualifica) = UPPER(%s)
               OR UPPER(cod_qualifica) = UPPER(%s)
            LIMIT 1
        """
        result = self.fetch_one(query_exact, (descrizione, alias_expanded, descrizione))
        if result:
            logger.info(
                f"Qualifica match esatto: '{descrizione}' -> "
                f"{result['des_qualifica']} (cod: {result['cod_qualifica']})"
            )
            self._set_cached_lookup("qualifica", descrizione_norm, result)
            return result

        # LIVELLO 2: Fuzzy matching (da cache)
        qualifiche = self._get_cached_ref_table(
            "qualifica", "SELECT cod_qualifica, des_qualifica FROM qualifica"
        )

        best_match = None
        best_score = 0.0

        for q in qualifiche:
            q_norm = self._normalize_name(q['des_qualifica'])
            score = self._calculate_similarity(descrizione_norm, q_norm)

            # Prova anche con alias
            score_alias = self._calculate_similarity(alias_expanded, q_norm)
            score = max(score, score_alias)

            if score > best_score:
                best_score = score
                best_match = q

        if best_match and best_score >= 0.70:
            logger.info(
                f"Qualifica fuzzy match: '{descrizione}' -> "
                f"{best_match['des_qualifica']} (cod: {best_match['cod_qualifica']}, "
                f"score: {best_score:.2f})"
            )
            self._set_cached_lookup("qualifica", descrizione_norm, best_match)
            return best_match

        logger.warning(f"Qualifica non trovata: {descrizione} (best score: {best_score:.2f})")
        self._set_cached_lookup("qualifica", descrizione_norm, None)
        return None

    def find_classe_cespiti_by_name(self, descrizione: str) -> dict | None:
        """
        Cerca una classe cespiti per descrizione con matching fuzzy.

        Args:
            descrizione: Descrizione della classe cespiti

        Returns:
            Dict con id_classe_cespiti e descrizione, o None
        """
        if not descrizione:
            return None

        descrizione_norm = self._normalize_name(descrizione)

        # Check cache lookup
        hit, cached = self._get_cached_lookup("classe_cespiti", descrizione_norm)
        if hit:
            return cached

        # LIVELLO 1: Match esatto
        query_exact = """
            SELECT id_classe_cespiti, descrizione
            FROM classe_cespiti
            WHERE UPPER(descrizione) = UPPER(%s)
            LIMIT 1
        """
        result = self.fetch_one(query_exact, (descrizione,))
        if result:
            logger.info(f"Classe cespiti match esatto: '{descrizione}' -> {result['descrizione']} (id: {result['id_classe_cespiti']})")
            self._set_cached_lookup("classe_cespiti", descrizione_norm, result)
            return result

        # LIVELLO 2: Fuzzy matching (da cache)
        classi = self._get_cached_ref_table(
            "classe_cespiti", "SELECT id_classe_cespiti, descrizione FROM classe_cespiti"
        )

        best_match = None
        best_score = 0.0

        for classe in classi:
            classe_norm = self._normalize_name(classe['descrizione'])
            score = self._calculate_similarity(descrizione_norm, classe_norm)

            if score > best_score:
                best_score = score
                best_match = classe

        if best_match and best_score >= 0.70:
            logger.info(f"Classe cespiti fuzzy match: '{descrizione}' -> {best_match['descrizione']} (id: {best_match['id_classe_cespiti']}, score: {best_score:.2f})")
            self._set_cached_lookup("classe_cespiti", descrizione_norm, best_match)
            return best_match

        logger.warning(f"Classe cespiti non trovata: {descrizione} (best score: {best_score:.2f})")
        self._set_cached_lookup("classe_cespiti", descrizione_norm, None)
        return None


def safe_value(value, max_length: int = None) -> str:
    """
    Escape e sanitizza un valore per SQL

    Args:
        value: Valore da sanitizzare
        max_length: Lunghezza massima (opzionale)

    Returns:
        Stringa safe per SQL
    """
    if value is None:
        return "NULL"

    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, (int, float)):
        return str(value)

    # Converti a stringa
    str_value = str(value)

    # Tronca se necessario
    if max_length and len(str_value) > max_length:
        str_value = str_value[:max_length]

    # Escape caratteri speciali
    str_value = str_value.replace("\\", "\\\\")
    str_value = str_value.replace("'", "\\'")
    str_value = str_value.replace('"', '\\"')
    str_value = str_value.replace("\n", "\\n")
    str_value = str_value.replace("\r", "\\r")
    str_value = str_value.replace("\t", "\\t")

    return f"'{str_value}'"
