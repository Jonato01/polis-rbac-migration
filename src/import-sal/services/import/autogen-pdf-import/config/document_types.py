"""
Document Types Configuration from Database
Carica configurazione tipi documento da DB e scopre i campi via DESCRIBE
"""
import json
import logging
import re
import time
from typing import Optional
from functools import lru_cache

import mysql.connector

logger = logging.getLogger(__name__)

# Cache con TTL
_document_types_cache: dict = {}
_table_schema_cache: dict = {}
_cache_loaded: bool = False
_cache_timestamp: float = 0
CACHE_TTL_SECONDS = 300  # 5 minuti — auto-refresh senza reload manuale

# Connessione persistente per config loading (1 sola per replica, non pool)
_config_connection: mysql.connector.MySQLConnection | None = None
_loading_in_progress: bool = False  # Previene retry ricorsivi


def _get_db_connection():
    """
    Ottiene connessione persistente al database per config loading.
    Riusa la stessa connessione tra chiamate. Se disconnessa, riconnette.
    1 connessione per replica (vs nuova connessione ogni volta = connection leak).
    """
    global _config_connection
    import os

    # Riusa connessione esistente se ancora viva
    if _config_connection is not None:
        try:
            _config_connection.ping(reconnect=True, attempts=1, delay=0)
            return _config_connection
        except Exception:
            try:
                _config_connection.close()
            except Exception:
                pass
            _config_connection = None

    db_host = os.getenv("DB_HOST", "localhost")
    db_port = int(os.getenv("DB_PORT", "3306"))
    db_user = os.getenv("DB_USER", "")
    db_password = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "polis")

    logger.info(f"Connessione DB config: host={db_host}, port={db_port}, user={db_user}, db={db_name}")

    if not db_name:
        logger.error("DB_NAME non impostato!")
        raise ValueError("DB_NAME environment variable is required")

    _config_connection = mysql.connector.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=db_name,
        charset='utf8mb4'
    )
    return _config_connection


def _get_table_schema(conn, table_name: str) -> list[dict]:
    """
    Ottiene lo schema di una tabella via DESCRIBE/information_schema.
    Se il nome non corrisponde esattamente, cerca tabelle simili.

    Returns:
        Lista di dict con: field, type, null, key, default, extra, comment
    """
    if table_name in _table_schema_cache:
        return _table_schema_cache[table_name]

    # Prima prova con il nome esatto
    cursor = None
    try:
        # Usa dictionary=True per avere dizionari invece di tuple
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"DESCRIBE `{table_name}`")
        columns = cursor.fetchall()

        if columns:
            # Ottieni anche i commenti delle colonne - usa cursor nuovo con dictionary=True
            cursor_comments = None
            try:
                cursor_comments = conn.cursor(dictionary=True)
                cursor_comments.execute("""
                    SELECT column_name, column_comment, data_type, 
                           character_maximum_length, is_nullable, column_key
                    FROM information_schema.columns 
                    WHERE table_schema = DATABASE() AND table_name = %s
                    ORDER BY ordinal_position
                """, (table_name,))

                # Gestisci sia dizionari che tuple per robustezza
                comments_rows = cursor_comments.fetchall()
                if comments_rows and isinstance(comments_rows[0], dict):
                    # Normalizza chiavi a lowercase (info_schema puo' restituire UPPER o lower)
                    comments_info = {}
                    for row in comments_rows:
                        row_lower = {k.lower(): v for k, v in row.items()}
                        comments_info[row_lower['column_name']] = row_lower
                else:
                    # Fallback per tuple (column_name, column_comment, data_type, max_length, is_nullable, column_key)
                    comments_info = {row[0]: {
                        'column_name': row[0],
                        'column_comment': row[1] if len(row) > 1 else '',
                        'data_type': row[2] if len(row) > 2 else '',
                        'character_maximum_length': row[3] if len(row) > 3 else None,
                        'is_nullable': row[4] if len(row) > 4 else 'YES',
                        'column_key': row[5] if len(row) > 5 else ''
                    } for row in comments_rows}
            except Exception as e:
                logger.warning(f"Errore recupero commenti per '{table_name}': {e}")
                comments_info = {}
            finally:
                if cursor_comments:
                    cursor_comments.close()

            schema = []
            for col in columns:
                field_name = col['Field']
                comment_info = comments_info.get(field_name, {})

                schema.append({
                    'field': field_name,
                    'type': col['Type'],
                    'null': col['Null'] == 'YES',
                    'key': col['Key'],
                    'default': col['Default'],
                    'extra': col['Extra'],
                    'comment': comment_info.get('column_comment', ''),
                    'data_type': comment_info.get('data_type', ''),
                    'max_length': comment_info.get('character_maximum_length')
                })

            _table_schema_cache[table_name] = schema
            logger.info(f"Schema tabella '{table_name}': {len(schema)} colonne")
            return schema

    except mysql.connector.Error as e:
        logger.warning(f"Tabella '{table_name}' non trovata esattamente: {e}")
    finally:
        if cursor:
            cursor.close()

    # Se non trovata, cerca tabelle simili
    logger.info(f"Cerco tabelle simili a '{table_name}'...")

    cursor_tables = conn.cursor()  # Nuovo cursore per sicurezza
    cursor_tables.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = DATABASE()
    """)

    all_tables = [row[0] for row in cursor_tables.fetchall()]
    cursor_tables.close()

    # Cerca per similarità
    table_name_lower = table_name.lower().replace('_', '')
    best_match = None
    best_score = 0

    for t in all_tables:
        t_lower = t.lower().replace('_', '')

        # Match esatto senza underscore
        if t_lower == table_name_lower:
            best_match = t
            break

        # Match parziale
        if table_name_lower in t_lower or t_lower in table_name_lower:
            score = len(set(table_name_lower) & set(t_lower))
            if score > best_score:
                best_score = score
                best_match = t

    if best_match and best_match != table_name:
        logger.info(f"Trovata tabella simile: '{best_match}' per '{table_name}'")
        # Richiama ricorsivamente con il nome corretto
        return _get_table_schema(conn, best_match)

    logger.error(f"Nessuna tabella trovata per '{table_name}'")
    return []


def _build_fields_config_from_schema(schema: list[dict]) -> dict:
    """
    Costruisce la configurazione dei campi dallo schema della tabella.
    Mappa i tipi MySQL ai tipi Python/JSON.
    """
    # Campi da escludere (gestiti automaticamente)
    exclude_fields = {'id', 'created_at', 'updated_at', 'pdf_path', 'path_file',
                      'nome_file', 'pdf_filename', 'numero_sal', 'sal'}

    fields = {}

    for col in schema:
        field_name = col['field']

        # Salta campi esclusi
        if field_name.lower() in exclude_fields:
            continue

        # Determina tipo
        mysql_type = col['type'].lower()
        data_type = col.get('data_type', '').lower()

        # Gestione ENUM
        if 'enum' in mysql_type or 'set' in mysql_type:
            field_type = 'enum'
            # Estrae valori permessi dalla definizione: enum('SI','NO')
            import re
            match = re.search(r"enum\((.*?)\)", mysql_type, re.IGNORECASE)
            if match:
                # Estrae i valori rimuovendo quotes
                enum_values_str = match.group(1)
                allowed_values = [v.strip().strip("'\"") for v in enum_values_str.split(',')]
            else:
                allowed_values = []
        elif any(t in mysql_type for t in ['int', 'tinyint', 'smallint', 'bigint']):
            if 'tinyint(1)' in mysql_type:
                field_type = 'boolean'
            else:
                field_type = 'integer'
        elif any(t in mysql_type for t in ['decimal', 'float', 'double', 'numeric']):
            field_type = 'number'
        elif any(t in mysql_type for t in ['date', 'datetime', 'timestamp']):
            field_type = 'date'
        else:
            field_type = 'string'

        # Descrizione dal commento o dal nome campo
        description = col.get('comment', '')
        if not description:
            # Genera descrizione dal nome campo
            description = field_name.replace('_', ' ').title()

        # Max length per stringhe
        max_length = col.get('max_length')

        # Required se NOT NULL e senza default
        required = not col['null'] and col['default'] is None and col['key'] != 'PRI'

        fields[field_name] = {
            'type': field_type,
            'description': description,
            'required': required
        }

        if max_length and field_type == 'string':
            fields[field_name]['max_length'] = max_length

        # Aggiungi valori permessi per ENUM
        if field_type == 'enum':
            fields[field_name]['allowed_values'] = allowed_values
            if col['default']:
                fields[field_name]['default'] = col['default']
            # Arricchisci la descrizione
            fields[field_name]['description'] += f" (valori: {', '.join(allowed_values)})"

    return fields


def load_document_types_from_db(force_reload: bool = False) -> dict:
    """
    Carica configurazione tipi documento dal database.
    Per ogni tipo, fa DESCRIBE delle tabelle per scoprire i campi.

    Usa cache con TTL: auto-refresh ogni CACHE_TTL_SECONDS senza reload esplicito.

    Returns:
        Dictionary con configurazione completa per ogni tipo documento
    """
    global _document_types_cache, _cache_loaded, _cache_timestamp

    now = time.time()

    # Cache valida: caricata E non forzata E TTL non scaduto
    if _cache_loaded and not force_reload and (now - _cache_timestamp) < CACHE_TTL_SECONDS:
        return _document_types_cache

    logger.info("Caricamento configurazione tipi documento da database...")

    conn = None

    try:
        conn = _get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Carica configurazione base
        cursor.execute("""
            SELECT
                doc_type_code,
                name,
                description,
                main_table,
                detail_table,
                filename_patterns,
                classification_hints,
                negative_hints,
                ocr_strategy,
                max_pages,
                check_rotation,
                vision_batch,
                extraction_model,
                extraction_notes
            FROM autogen_document_types
            WHERE is_active = 1
            ORDER BY doc_type_code
        """)

        rows = cursor.fetchall()
        cursor.close()

        config = {}

        for row in rows:
            doc_type = row['doc_type_code']
            main_table = row['main_table']
            detail_table = row['detail_table']

            # Ottieni schema tabella principale
            main_schema = _get_table_schema(conn, main_table)
            main_fields = _build_fields_config_from_schema(main_schema)

            # Ottieni schema tabella dettaglio se presente + FK dinamica
            detail_fields = None
            detail_fk_column = None
            if detail_table:
                detail_schema = _get_table_schema(conn, detail_table)
                detail_fields = _build_fields_config_from_schema(detail_schema)
                detail_fk_column = _get_detail_fk_column(conn, detail_table, main_table)

            # Parse JSON
            patterns = json.loads(row['filename_patterns']) if row['filename_patterns'] else []
            hints = json.loads(row['classification_hints']) if row['classification_hints'] else []
            negative_hints = json.loads(row['negative_hints']) if row.get('negative_hints') else []

            # Genera SQL template automaticamente
            sql_template = _generate_sql_template(main_table, main_schema)
            detail_sql_template = None
            if detail_table and detail_fields:
                detail_sql_template = _generate_sql_template(detail_table, _get_table_schema(conn, detail_table))

            config[doc_type] = {
                "name": row['name'],
                "description": row['description'] or "",
                "tables": {
                    "main": main_table,
                    "detail": detail_table
                },
                "ocr_strategy": row['ocr_strategy'] or "mistral",
                "max_pages": row['max_pages'] or 10,
                "check_rotation": bool(row.get('check_rotation', 0)),
                "vision_batch": bool(row.get('vision_batch', 1)),
                "filename_patterns": patterns,
                "classification_hints": hints,
                "negative_hints": negative_hints,
                "fields": main_fields,
                "detail_fields": detail_fields,
                "sql_template": sql_template,
                "detail_sql_template": detail_sql_template,
                "detail_fk_column": detail_fk_column,
                "extraction_model": row.get('extraction_model', 'claude-sonnet-4-5'),
                "extraction_notes": row['extraction_notes'],
                "schema": main_schema,
                "detail_schema": _get_table_schema(conn, detail_table) if detail_table else None
            }

            logger.debug(f"Caricato tipo '{doc_type}': tabella {main_table}, {len(main_fields)} campi")

        _document_types_cache = config
        _cache_loaded = True
        _cache_timestamp = time.time()

        logger.info(f"Caricati {len(config)} tipi documento dal database")
        return config

    except mysql.connector.Error as e:
        logger.error(f"Errore caricamento configurazione da DB: {e}")
        if _document_types_cache:
            logger.warning("Uso cache esistente")
            return _document_types_cache

        # Nessuna cache e non siamo già in retry: retry con backoff
        if not _loading_in_progress:
            _loading_in_progress = True
            try:
                for retry in range(3):
                    wait = 2 ** retry  # 1s, 2s, 4s
                    logger.warning(f"Retry config loading {retry + 1}/3 (attesa {wait}s)...")
                    time.sleep(wait)
                    try:
                        _config_connection = None
                        _cache_loaded = False
                        return load_document_types_from_db(force_reload=True)
                    except Exception as retry_err:
                        logger.error(f"Retry {retry + 1}/3 fallito: {retry_err}")
            finally:
                _loading_in_progress = False

        return {}


def _get_detail_fk_column(conn, detail_table: str, main_table: str) -> str | None:
    """
    Trova la colonna FK nella tabella dettaglio che referenzia la tabella principale.
    Usa information_schema.KEY_COLUMN_USAGE per rilevamento automatico.

    Returns:
        Nome colonna FK (es. 'id_ordine' per BDC, 'id_fattura' per FATTURE) o None
    """
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
            LIMIT 1
        """, (detail_table, main_table))

        row = cursor.fetchone()
        if row:
            fk_col = row['COLUMN_NAME']
            logger.info(f"FK rilevata: {detail_table}.{fk_col} -> {main_table}")
            return fk_col

        logger.warning(f"Nessuna FK trovata: {detail_table} -> {main_table}")
        return None

    except Exception as e:
        logger.warning(f"Errore rilevamento FK {detail_table} -> {main_table}: {e}")
        return None
    finally:
        if cursor:
            cursor.close()


def _generate_sql_template(table_name: str, schema: list[dict]) -> str:
    """
    Genera automaticamente il template SQL INSERT basandosi sullo schema.
    """
    # Campi da escludere dall'INSERT (auto-generati)
    exclude_fields = {'id', 'created_at', 'updated_at'}

    # Campi speciali gestiti separatamente
    special_mappings = {
        'pdf_path': '{pdf_path}',
        'path_file': '{pdf_path}',
        'nome_file': '{pdf_filename}',
        'pdf_filename': '{pdf_filename}',
        'numero_sal': '{sal_number}',
        'sal': '{sal_number}'
    }

    columns = []
    values = []
    updates = []

    for col in schema:
        field_name = col['field']

        if field_name.lower() in exclude_fields:
            continue

        columns.append(f"`{field_name}`")

        # Valore placeholder
        if field_name.lower() in special_mappings:
            values.append(special_mappings[field_name.lower()])
        else:
            values.append(f"{{{field_name}}}")

        # Per UPDATE (escludi chiavi primarie)
        if col['key'] != 'PRI' and field_name.lower() not in special_mappings:
            updates.append(f"`{field_name}` = VALUES(`{field_name}`)")

    sql = f"INSERT INTO `{table_name}` ({', '.join(columns)}) VALUES ({', '.join(values)})"

    if updates:
        # id = LAST_INSERT_ID(id) fa sì che MySQL imposti lastrowid all'id del record
        # esistente anche quando ON DUPLICATE KEY UPDATE aggiorna invece di inserire.
        # Senza questo, lastrowid torna 0 e le FK delle righe dettaglio si rompono.
        sql += f" ON DUPLICATE KEY UPDATE `id` = LAST_INSERT_ID(`id`), {', '.join(updates)}"

    return sql


def get_document_config(doc_type: str) -> Optional[dict]:
    """Ritorna configurazione per un tipo documento specifico"""
    config = load_document_types_from_db()
    return config.get(doc_type)


def get_all_document_types() -> list[str]:
    """Ritorna lista di tutti i tipi documento configurati"""
    config = load_document_types_from_db()
    return list(config.keys())


def get_classification_hints() -> dict[str, list[str]]:
    """Ritorna hints per classificazione per tutti i tipi"""
    config = load_document_types_from_db()
    return {
        doc_type: conf.get("classification_hints", [])
        for doc_type, conf in config.items()
    }


def reload_config():
    """Forza il ricaricamento della configurazione dal database"""
    global _cache_loaded, _table_schema_cache
    _cache_loaded = False
    _table_schema_cache = {}
    return load_document_types_from_db(force_reload=True)


def search_table_by_name(partial_name: str) -> list[str]:
    """
    Cerca tabelle nel database che matchano un nome parziale.
    Utile per debug e configurazione.
    """
    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = DATABASE()
              AND table_name LIKE %s
            ORDER BY table_name
        """, (f'%{partial_name}%',))

        tables = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return tables

    finally:
        if conn:
            conn.close()


class DocumentTypesConfig:
    """Classe wrapper per accesso alla configurazione come dizionario"""

    def __getitem__(self, key):
        return load_document_types_from_db().get(key)

    def __contains__(self, key):
        return key in load_document_types_from_db()

    def get(self, key, default=None):
        return load_document_types_from_db().get(key, default)

    def keys(self):
        return load_document_types_from_db().keys()

    def values(self):
        return load_document_types_from_db().values()

    def items(self):
        return load_document_types_from_db().items()

    def __iter__(self):
        return iter(load_document_types_from_db())

    def __len__(self):
        return len(load_document_types_from_db())


# Istanza singleton per accesso come dizionario
DOCUMENT_TYPES_CONFIG = DocumentTypesConfig()

