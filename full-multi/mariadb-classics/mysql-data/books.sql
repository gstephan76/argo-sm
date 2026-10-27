-- Brazilian Portuguese literature, 20 representative classics.
-- Only on a fresh MariaDB 10.11 data directory (SCL mysql-init hook).
-- bookdb is created by MYSQL_DATABASE in the Rollout.
-- IDs 1-3 deliberately overlap the Go Backend INSERT IGNORE demo rows.
SET NAMES utf8mb4;
CREATE TABLE IF NOT EXISTS books (
    id INT PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    author VARCHAR(200) NOT NULL,
    year INT NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
INSERT INTO books (id, title, author, year) VALUES
    (1, 'Memórias Póstumas de Brás Cubas', 'Machado de Assis', 1881),
    (2, 'Dom Casmurro', 'Machado de Assis', 1899),
    (3, 'O Cortiço', 'Aluísio Azevedo', 1890),
    (4, 'Iracema', 'José de Alencar', 1865),
    (5, 'O Guarani', 'José de Alencar', 1857),
    (6, 'Os Sertões', 'Euclides da Cunha', 1902),
    (7, 'Triste Fim de Policarpo Quaresma', 'Lima Barreto', 1915),
    (8, 'Macunaíma', 'Mário de Andrade', 1928),
    (9, 'Vidas Secas', 'Graciliano Ramos', 1938),
    (10, 'São Bernardo', 'Graciliano Ramos', 1934),
    (11, 'Capitães da Areia', 'Jorge Amado', 1937),
    (12, 'O Quinze', 'Rachel de Queiroz', 1930),
    (13, 'Grande Sertão: Veredas', 'João Guimarães Rosa', 1956),
    (14, 'Quarto de Despejo', 'Carolina Maria de Jesus', 1960),
    (15, 'A Hora da Estrela', 'Clarice Lispector', 1977),
    (16, 'A Paixão Segundo G.H.', 'Clarice Lispector', 1964),
    (17, 'O Continente (O Tempo e o Vento)', 'Erico Verissimo', 1949),
    (18, 'Morte e Vida Severina', 'João Cabral de Melo Neto', 1955),
    (19, 'Romanceiro da Inconfidência', 'Cecília Meireles', 1953),
    (20, 'A Moreninha', 'Joaquim Manuel de Macedo', 1844);
