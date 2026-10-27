# Executed (sourced) by the sclorg MariaDB 10.11 mysql-init hook.
# mysql_flags and MYSQL_DATADIR_FIRST_INIT come from the base-image entrypoint.
# Only seed brand-new database directories, never an existing active database.
if ! [ -v MYSQL_RUNNING_AS_SLAVE ] && "$MYSQL_DATADIR_FIRST_INIT"; then
    log_info "Seeding 20 Brazilian literary classics into ${MYSQL_DATABASE}..."
    mysql $mysql_flags "${MYSQL_DATABASE}" < /opt/app-root/src/mysql-data/books.sql
fi
