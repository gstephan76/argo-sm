# Sourced by sclorg MariaDB 10.11 mysql-init hook.
# Do not overwrite books in an existing data directory.
if ! [ -v MYSQL_RUNNING_AS_SLAVE ] && "$MYSQL_DATADIR_FIRST_INIT"; then
    log_info "Seeding 20 English literary classics into ${MYSQL_DATABASE}..."
    mysql $mysql_flags "${MYSQL_DATABASE}" < /opt/app-root/src/mysql-data/books.sql
fi
