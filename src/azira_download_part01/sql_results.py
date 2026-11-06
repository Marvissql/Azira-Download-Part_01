"""Consume SQL Server results so deferred errors reach the caller."""


def drain_results(cursor, on_rows=None):
    while True:
        if cursor.description:
            columns = tuple(column[0] for column in cursor.description)
            while rows := cursor.fetchmany(1000):
                if on_rows is not None:
                    on_rows(columns, rows)
        if not cursor.nextset():
            break