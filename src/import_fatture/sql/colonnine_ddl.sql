-- DDL per tabelle colonnine (stazioni di ricarica)
-- Eseguire su database polis (dev e prod)

CREATE TABLE IF NOT EXISTS colonnine (
    id_colonnina INT AUTO_INCREMENT PRIMARY KEY,
    id_sal INT NOT NULL,
    regione VARCHAR(100),
    provincia VARCHAR(100),
    comune VARCHAR(255),
    nr_idr_installate INT,
    nr_idr_allacciate INT,
    data_importazione DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (id_sal) REFERENCES sal(id_sal),
    UNIQUE KEY uk_colonnina_sal (id_sal, regione, provincia, comune)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS colonnine_id_pun (
    id_pun_entry INT AUTO_INCREMENT PRIMARY KEY,
    id_colonnina INT NOT NULL,
    id_piattaforma VARCHAR(100) NOT NULL,
    FOREIGN KEY (id_colonnina) REFERENCES colonnine(id_colonnina) ON DELETE CASCADE,
    UNIQUE KEY uk_pun (id_colonnina, id_piattaforma)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS colonnine_bdc (
    id_bdc_entry INT AUTO_INCREMENT PRIMARY KEY,
    id_colonnina INT NOT NULL,
    numero_bdc VARCHAR(50) NOT NULL,
    FOREIGN KEY (id_colonnina) REFERENCES colonnine(id_colonnina) ON DELETE CASCADE,
    UNIQUE KEY uk_bdc (id_colonnina, numero_bdc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
