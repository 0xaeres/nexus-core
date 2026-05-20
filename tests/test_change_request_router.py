from nexus.council.change_request import route


def test_security_kind_routes_to_sentinel() -> None:
    assert route("security") == "security_sentinel"


def test_tech_stack_routes_to_archaeologist() -> None:
    assert route("tech_stack") == "archaeologist"


def test_language_routes_to_archaeologist() -> None:
    assert route("language") == "archaeologist"


def test_unknown_kind_falls_back_to_archaeologist() -> None:
    assert route("master") == "archaeologist"
    assert route("product_domain") == "archaeologist"
    assert route("garbage") == "archaeologist"
