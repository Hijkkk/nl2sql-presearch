if __name__ == '__main__':
    metadata: dict[str, list[dict]] = {
        "tables": [
            {}
        ]
    }
    tables = metadata.get("tables", [])
    print(type(tables))
    relevant_tables:list[str] = ["names"]
    # tables = [t for t in tables if t["name"] in relevant_tables]
    print()
