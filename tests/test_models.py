from book_tools.models import Book, SearchQuery, filter_books


def test_language_aliases_and_regions_do_not_discard_english_originals():
    books = [Book("annas", "1", "a", "English edition", language="English [en-US]", extension="epub"),
             Book("zlib", "2", "b", "Chinese translation", language="chinese", extension="epub"),
             Book("zlib", "3", "c", "Unknown edition", extension="epub")]
    selected = filter_books(books, SearchQuery("edition", language="english", extension="EPUB"))
    assert [book.id for book in selected] == ["1"]


def test_year_filters_are_inclusive_and_unknown_year_is_not_a_match():
    books = [Book("zlib", str(year), "h", "Edition", year=year) for year in ("1999", "2000", "2001", "unknown")]
    selected = filter_books(books, SearchQuery("edition", year_from=2000, year_to=2001))
    assert [book.year for book in selected] == ["2000", "2001"]
