-- =====================================================
-- Script creazione tabella configurazione tipi documento
-- per AutoGen PDF Import Service
--
-- APPROCCIO SEMPLIFICATO:
-- - Solo nome tabelle nella configurazione
-- - Il sistema fa DESCRIBE automatico per scoprire i campi
-- - Se il nome tabella non è esatto, cerca tra tutte le tabelle
-- =====================================================

-- Tabella configurazione tipi documento (SEMPLIFICATA)
CREATE TABLE IF NOT EXISTS autogen_document_types (
    id INT AUTO_INCREMENT PRIMARY KEY,
    doc_type_code VARCHAR(50) NOT NULL UNIQUE COMMENT 'Codice tipo documento (es. AQ, FATTURA)',
    name VARCHAR(100) NOT NULL COMMENT 'Nome visualizzato',
    description TEXT COMMENT 'Descrizione del tipo documento',

    -- Tabelle destinazione (solo nomi, il sistema fa DESCRIBE)
    main_table VARCHAR(100) NOT NULL COMMENT 'Nome tabella principale',
    detail_table VARCHAR(100) COMMENT 'Nome tabella dettaglio/righe (opzionale)',

    -- Pattern per riconoscimento da filename
    filename_patterns JSON COMMENT 'Array regex per estrazione dati da nome file',

    -- Hints per classificazione automatica
    classification_hints JSON COMMENT 'Parole chiave per classificazione automatica',

    -- Configurazione OCR
    ocr_strategy ENUM('mistral', 'document_intelligence', 'both') DEFAULT 'mistral' COMMENT 'Strategia OCR',
    max_pages INT DEFAULT 10 COMMENT 'Max pagine da processare',

    -- Note aggiuntive per estrazione
    extraction_notes TEXT COMMENT 'Note aggiuntive per prompt estrazione GPT',

    -- Stato
    is_active TINYINT(1) DEFAULT 1 COMMENT 'Flag attivo/disattivo',

    -- Timestamp
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_doc_type_active (doc_type_code, is_active),
    INDEX idx_main_table (main_table)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Configurazione tipi documento - il sistema scopre i campi automaticamente via DESCRIBE';


-- =====================================================
-- POPOLAMENTO DATI INIZIALI
-- =====================================================

-- 1. AQ - Accordi Quadro
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'AQ',
    'Accordo Quadro',
    'Contratti quadro con fornitori',
    'pdf_aq',
    NULL,
    '["AQ[_\\\\-]?(\\\\d{10})", "(\\\\d{10})[_\\\\-]AQ", "Accordo[_\\\\-]?Quadro[_\\\\-]?(\\\\d+)"]',
    '["AQ_", "Accordo_Quadro", "Accordo Quadro", "contratto quadro"]',
    'mistral',
    10,
    'Il codice AQ potrebbe essere mascherato nel documento (es. 449000xxxx). Se vedi un codice mascherato, lascialo così - verrà integrato dal nome file.'
);

-- 2. BDC - Buoni di Consegna
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'BDC',
    'Buono di Consegna',
    'Documenti consegna merce/servizi',
    'bdc_pdf',
    'righe_bdc_pdf',
    '["BDC[_\\\\-]?(\\\\d+)", "Buono[_\\\\-]?Consegna[_\\\\-]?(\\\\d+)", "ODA[_\\\\-]?(\\\\d+)"]',
    '["BDC_", "Buono_Consegna", "Buono di Consegna", "ODA", "Ordine di Acquisto"]',
    'mistral',
    5,
    NULL
);

-- 3. FATTURA
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'FATTURA',
    'Fattura',
    'Fatture fornitori (TD01-TD28)',
    'fatture_pdf',
    'righe_fattura_pdf',
    '["Fattura[_\\\\-]?(\\\\d+)", "FE[_\\\\-]?(\\\\d+)", "(\\\\d+)[_\\\\-]fattura"]',
    '["Fattura_", "FE_", "fattura", "nota di credito", "TD01", "TD04"]',
    'mistral',
    20,
    'Estrai tipo documento (TD01, TD04, etc.) se presente. Il protocollo è un campo importante.'
);

-- 4. IVA_INDETRAIBILE
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'IVA_INDETRAIBILE',
    'IVA Indetraibile',
    'Documenti IVA indetraibile con periodi e percentuali',
    'indetraibilita_iva_pdf',
    NULL,
    '["IVA[_\\\\-]?Indetr", "Indetraibilita"]',
    '["IVA_Indetr", "Indetraibilita", "IVA indetraibile", "percentuale indetraibilità"]',
    'document_intelligence',
    10,
    'Questo documento contiene tabelle con percentuali. Usa Document Intelligence per migliore estrazione tabelle.'
);

-- 5. LETTERA_INCARICO
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'LETTERA_INCARICO',
    'Lettera di Incarico',
    'Lettere di incarico dipendenti',
    'pdf_lettere_incarico',
    NULL,
    '["Lettera[_\\\\-]?Incarico[_\\\\-]?(.+)", "LI[_\\\\-](.+)", "Incarico[_\\\\-](.+)"]',
    '["Lettera_Incarico", "LI_", "lettera di incarico", "conferimento incarico"]',
    'mistral',
    3,
    'Estrai nome e cognome del dipendente, livello (A1, A2, B, C, D, E, DIRIGENTE) e data decorrenza.'
);

-- 6. NOTA_METODOLOGICA
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'NOTA_METODOLOGICA',
    'Nota Metodologica',
    'Note metodologiche SAL con costi del personale',
    'nota_metodologica_pdf',
    NULL,
    '["Nota[_\\\\-]?Metodologica", "NM[_\\\\-]", "costi[_\\\\-]?personale"]',
    '["Nota_Metodologica", "NM_", "nota metodologica", "costi del personale", "tabella costi"]',
    'mistral',
    5,
    'Documento con tabelle costi personale per livello. Estrai tutti i valori numerici delle varie voci.'
);

-- 7. NOTA_TRASMISSIONE
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'NOTA_TRASMISSIONE',
    'Nota Trasmissione',
    'Note trasmissione rendicontazione per linea intervento',
    'pdf_rendicontazione_polis_sal',
    NULL,
    '["Nota[_\\\\-]?Trasmissione", "NT[_\\\\-]", "Rendicontazione"]',
    '["Nota_Trasmissione", "NT_", "nota di trasmissione", "rendicontazione", "linea intervento"]',
    'mistral',
    5,
    'Estrai numero SAL, periodo (inizio/fine), linea intervento e importi rendicontati.'
);

-- 8. PASSAGGIO_LIVELLO
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'PASSAGGIO_LIVELLO',
    'Passaggio Livello',
    'Comunicazioni passaggio livello dipendenti',
    'passaggi_livello_sal',
    NULL,
    '["Passaggio[_\\\\-]?Livello[_\\\\-]?(.+)", "PL[_\\\\-](.+)"]',
    '["Passaggio_Livello", "PL_", "passaggio livello", "cambio livello", "promozione"]',
    'mistral',
    3,
    'Estrai nome cognome dipendente, nuovo livello e data decorrenza del passaggio.'
);

-- 9. TIMESHEET
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'TIMESHEET',
    'Timesheet',
    'Fogli presenze dipendenti con ore per mese',
    'pdf_timesheet',
    NULL,
    '["Timesheet[_\\\\-]?(.+)", "TM[_\\\\-](.+)", "Foglio[_\\\\-]?Presenze[_\\\\-]?(.+)"]',
    '["Timesheet", "TM_", "Foglio_Presenze", "foglio presenze", "ore lavorate"]',
    'both',
    20,
    'I timesheet possono essere ruotati. Estrai nome dipendente, mese, anno e totale ore. Usa entrambi i provider OCR.'
);

-- 10. VAP
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'VAP',
    'VAP',
    'Verbali Accettazione Provvisoria',
    'vap_pdf',
    'vap_pdf_posizioni',
    '["VAP[_\\\\-]?(\\\\d+)", "Verbale[_\\\\-]?Accettazione[_\\\\-]?(\\\\d+)"]',
    '["VAP_", "Verbale_Accettazione", "verbale accettazione provvisoria"]',
    'mistral',
    10,
    'Estrai numero VAP, fornitore, importi e tutte le posizioni con relativi dettagli.'
);


-- =====================================================
-- ESEMPIO: Come aggiungere un nuovo tipo documento
-- =====================================================
-- Per aggiungere un nuovo tipo documento:
-- 1. Creare la tabella destinazione nel DB (se non esiste)
-- 2. Inserire record qui sotto
-- 3. Il sistema automaticamente:
--    - Fa DESCRIBE della tabella per scoprire i campi
--    - Genera il prompt di estrazione basandosi sui nomi/commenti colonne
--    - Genera la query INSERT automaticamente
--
-- NESSUNA modifica al codice Python necessaria!

-- Esempio: aggiunta tipo "CONTRATTO"
/*
-- Prima creo la tabella
CREATE TABLE contratti_pdf (
    id INT AUTO_INCREMENT PRIMARY KEY,
    numero_contratto VARCHAR(50) COMMENT 'Numero identificativo contratto',
    data_contratto DATE COMMENT 'Data stipula contratto',
    contraente VARCHAR(255) COMMENT 'Nome della controparte',
    oggetto TEXT COMMENT 'Oggetto del contratto',
    importo DECIMAL(15,2) COMMENT 'Importo totale',
    durata_mesi INT COMMENT 'Durata in mesi',
    pdf_path VARCHAR(500),
    numero_sal INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Poi aggiungo la configurazione
INSERT INTO autogen_document_types (
    doc_type_code, name, description,
    main_table, detail_table,
    filename_patterns, classification_hints,
    ocr_strategy, max_pages, extraction_notes
) VALUES (
    'CONTRATTO',
    'Contratto',
    'Contratti generici',
    'contratti_pdf',
    NULL,
    '["Contratto[_\\\\-]?(\\\\d+)", "CTR[_\\\\-]?(\\\\d+)"]',
    '["Contratto_", "CTR_", "contratto", "accordo contrattuale"]',
    'mistral',
    30,
    'Estrai numero contratto, data, contraente e importo.'
);

-- FATTO! Il sistema ora gestisce automaticamente i documenti "CONTRATTO"
*/

