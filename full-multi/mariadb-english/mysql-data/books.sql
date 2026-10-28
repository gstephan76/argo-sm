-- BLUE baseline: 20 British English-language literary classics.
-- Only loaded on first initialization of an empty MariaDB data directory.
-- IDs 1-3 supersede the CRUD application's INSERT IGNORE fallback demo books.
SET NAMES utf8mb4;
CREATE TABLE IF NOT EXISTS books (
    id INT PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    author VARCHAR(200) NOT NULL,
    year INT NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
INSERT INTO books (id, title, author, year) VALUES
    (1, 'Pride and Prejudice', 'Jane Austen', 1813),
    (2, 'Sense and Sensibility', 'Jane Austen', 1811),
    (3, 'Emma', 'Jane Austen', 1815),
    (4, 'Jane Eyre', 'Charlotte Brontë', 1847),
    (5, 'Wuthering Heights', 'Emily Brontë', 1847),
    (6, 'Great Expectations', 'Charles Dickens', 1861),
    (7, 'Oliver Twist', 'Charles Dickens', 1838),
    (8, 'A Tale of Two Cities', 'Charles Dickens', 1859),
    (9, 'David Copperfield', 'Charles Dickens', 1850),
    (10, 'Middlemarch', 'George Eliot', 1871),
    (11, 'Tess of the d''Urbervilles', 'Thomas Hardy', 1891),
    (12, 'Far from the Madding Crowd', 'Thomas Hardy', 1874),
    (13, 'Frankenstein', 'Mary Shelley', 1818),
    (14, 'The Time Machine', 'H. G. Wells', 1895),
    (15, 'The War of the Worlds', 'H. G. Wells', 1898),
    (16, 'A Room with a View', 'E. M. Forster', 1908),
    (17, 'Mrs Dalloway', 'Virginia Woolf', 1925),
    (18, 'To the Lighthouse', 'Virginia Woolf', 1927),
    (19, 'Nineteen Eighty-Four', 'George Orwell', 1949),
    (20, 'Animal Farm', 'George Orwell', 1945);
