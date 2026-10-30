# POLIS Import Service - Comunicazione Service Bus

## Panoramica

Il servizio di import gira come Azure Container App e comunica tramite **Azure Service Bus** con un pattern request/response asincrono su due code separate.

---

## Code Service Bus

| Coda | Direzione | Nome default | Variabile d'ambiente |
|------|-----------|--------------|----------------------|
| **Input** | Client -> Container App | `import-requests` | `SERVICEBUS_INPUT_QUEUE` |
| **Output** | Container App -> Client | `import-results` | `SERVICEBUS_OUTPUT_QUEUE` |

I nomi delle code sono configurabili tramite variabili d'ambiente. Se non specificati, vengono usati i valori di default.

---

## Messaggio di Input (Richiesta)

Inviare un messaggio JSON sulla coda **`import-requests`**.

### Schema

```json
{
  "upload_id": 42,
  "folder_path": "9_SAL_31_08_2025",
  "sal_number": 9,
  "task": "import-excel",
  "import_type": "excel",
  "priority": "normal",
  "force_reprocess": false
}
```

### Campi

| Campo | Tipo | Obbligatorio | Default | Descrizione |
|-------|------|:------------:|---------|-------------|
| `upload_id` | `int` | **Si** | - | ID della richiesta di upload (intero). |
| `folder_path` | `string` | **Si** | - | Nome della cartella SAL su Blob Storage (es. `9_SAL_31_08_2025`). Corrisponde al prefisso blob da scansionare. |
| `sal_number` | `int` | **Si** | - | Numero SAL esplicito (es. `9`). |
| `task` | `string` | No | `"import-excel"` | Tipo di task da eseguire. |
| `import_type` | `string` | No | `"excel"` | Tipo di import (es. `excel`). |
| `priority` | `string` | No | `"normal"` | Priorita della richiesta (es. `normal`, `high`). |
| `force_reprocess` | `boolean` | No | `false` | Se `true`, rielabora anche i file gia importati con successo. Se `false`, i file con metadata `import_status=success` vengono saltati. |

### Esempio minimo

```json
{
  "upload_id": 42,
  "folder_path": "9_SAL_31_08_2025",
  "sal_number": 9
}
```

### Esempio completo (forzare rielaborazione)

```json
{
  "upload_id": 42,
  "folder_path": "9_SAL_31_08_2025",
  "sal_number": 9,
  "task": "import-excel",
  "import_type": "excel",
  "priority": "high",
  "force_reprocess": true
}
```

---

## Messaggio di Output (Risposta)

Il servizio invia il risultato sulla coda **`import-results`** al termine dell'elaborazione.

### Schema

```json
{
  "import_type": "excel",
  "upload_id": 42,
  "status": "success",
  "summary": {
    "total_files": 5,
    "success": 3,
    "failed": 1,
    "skipped": 1,
    "errors": 2
  },
  "failed_files": [
    {
      "blob_path": "9_SAL_31_08_2025/gare/gare_sal9.xlsx",
      "classified_as": "gare",
      "error": "Eccezione durante import: Connection refused"
    }
  ]
}
```

### Campi del messaggio principale

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `import_type` | `string` | Tipo di import (`excel`, `pdf`). Corrisponde al valore della richiesta. |
| `upload_id` | `int` | Corrisponde al `upload_id` della richiesta (per correlazione). |
| `status` | `string` | Status complessivo dell'elaborazione (vedi tabella sotto). |
| `summary` | `object` | Riepilogo contatori (vedi sotto). |
| `failed_files` | `array` | Lista dei file falliti. Vuota se tutto ok. |

### Campi di `summary`

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `total_files` | `int` | Numero totale di file Excel trovati nella cartella. |
| `success` | `int` | File importati con successo. |
| `failed` | `int` | File il cui import e fallito. |
| `skipped` | `int` | File saltati (gia elaborati o tipo non riconosciuto). |
| `errors` | `int` | Numero totale di errori a livello riga (somma di tutti i file). |

### Valori di `status` (risposta)

| Valore | Significato |
|--------|-------------|
| `success` | Tutti i file processati con successo (o tutti skippati, nessun fallimento). |
| `completed_with_errors` | Almeno un file fallito o errore fatale. |

### Campi di ogni elemento in `failed_files`

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `blob_path` | `string` | Percorso completo del blob su Azure Storage. |
| `classified_as` | `string` | Tipo rilevato dall'AI: `fatture`, `personale`, `servizi`, `gare`, `rendicontazione`, `unknown`. |
| `error` | `string\|null` | Messaggio di errore. |

---

## Flusso di Elaborazione

```
Client                    Service Bus                   Container App
  |                           |                              |
  |-- JSON request ---------> |                              |
  |                           | -- import-requests --------> |
  |                           |                              |
  |                           |   1. Completa il messaggio   |
  |                           |      (rimuove dalla coda)    |
  |                           |                              |
  |                           |   2. Scansiona Blob Storage  |
  |                           |      (cartella SAL)          |
  |                           |                              |
  |                           |   3. Filtra file gia'        |
  |                           |      elaborati (se non       |
  |                           |      force_reprocess)        |
  |                           |                              |
  |                           |   4. Scarica + classifica    |
  |                           |      ogni file con AI        |
  |                           |                              |
  |                           |   5. Importa nell'ordine:    |
  |                           |      fatture -> personale -> |
  |                           |      servizi -> gare ->      |
  |                           |      rendicontazione         |
  |                           |                              |
  |                           |   6. Tagga blob con metadata |
  |                           |                              |
  |                           | <-- import-results --------- |
  | <-- JSON response ------- |                              |
```

### Note sul flusso

- **Completamento immediato**: il messaggio di input viene rimosso dalla coda (`complete_message`) **prima** di iniziare l'elaborazione. Questo evita problemi di lock expiration su elaborazioni lunghe (diversi minuti).
- **Messaggi malformati**: se il JSON non e valido o mancano i campi obbligatori (`upload_id`, `folder_path`, `sal_number`), il messaggio viene spostato nella **dead-letter queue** della coda di input.
- **Ordine di import**: i file vengono importati in ordine fisso (`fatture` -> `personale` -> `servizi` -> `gare` -> `rendicontazione`) per rispettare eventuali dipendenze tra dati.
- **Idempotenza**: ri-inviando la stessa richiesta, i file gia importati con successo vengono saltati automaticamente (a meno che `force_reprocess=true`).

---

## Metadata sui Blob

Dopo l'elaborazione, ogni blob Excel viene taggato con metadata per tracciabilita e idempotenza:

| Chiave metadata | Descrizione |
|-----------------|-------------|
| `import_status` | `success`, `failed`, `skipped`, `unknown_type` |
| `import_type` | `fatture`, `personale`, `servizi`, `gare`, `rendicontazione`, `unknown` |
| `import_date` | Timestamp ISO 8601 dell'elaborazione |
| `import_request_id` | `upload_id` della richiesta che ha processato il file |
| `import_sal_number` | Numero SAL associato al file |
| `import_error` | Messaggio di errore (solo se fallito, max 256 caratteri) |

Questi metadata vengono usati per decidere se un file e gia stato elaborato: un blob con `import_status=success` viene saltato nelle esecuzioni successive.

---

## Gestione Errori

| Scenario | Comportamento |
|----------|---------------|
| JSON malformato / campi obbligatori mancanti | Messaggio spostato in **dead-letter queue** |
| Nessun file Excel nella cartella | Risposta con `status=completed`, `files_found=0` |
| File non classificabile dall'AI | File saltato, `classified_as=unknown`, `status=skipped` |
| Errore durante import di un file | File marcato `failed`, gli altri continuano |
| Tutti i file falliti | Risposta con `status=failed` |
| Mix successi/fallimenti | Risposta con `status=partial` |
| Eccezione fatale (crash) | Risposta con `status=failed` (se possibile) |
| Errore Service Bus | Il messaggio torna disponibile dopo il lock timeout |

---

## Esempio di Invio con Azure CLI

```bash
# Inviare una richiesta di import
az servicebus queue send \
  --namespace-name <namespace> \
  --queue-name import-requests \
  --body '{"upload_id": 42, "folder_path": "9_SAL_31_08_2025", "sal_number": 9}'

# Inviare con force_reprocess
az servicebus queue send \
  --namespace-name <namespace> \
  --queue-name import-requests \
  --body '{"upload_id": 42, "folder_path": "9_SAL_31_08_2025", "sal_number": 9, "force_reprocess": true, "priority": "high"}'

# Leggere i risultati (peek senza rimuovere)
az servicebus queue peek \
  --namespace-name <namespace> \
  --queue-name import-results \
  --max-count 5
```

---

## Configurazione Ambiente

Variabili d'ambiente necessarie per la connessione Service Bus:

```env
AZURE_SERVICEBUS_CONNECTION_STRING=Endpoint=sb://<namespace>.servicebus.windows.net/;SharedAccessKeyName=...;SharedAccessKey=...
SERVICEBUS_INPUT_QUEUE=import-requests
SERVICEBUS_OUTPUT_QUEUE=import-results
```
