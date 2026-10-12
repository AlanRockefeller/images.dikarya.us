import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from images_app import gallery
from images_app.models import Image


@pytest.fixture
def tree(make):
    amanita = make.taxon(48419, "Amanita", "genus")
    muscaria = make.taxon(48715, "Amanita muscaria", parent=amanita)
    phalloides = make.taxon(48702, "Amanita phalloides", parent=amanita)
    cantharellus = make.taxon(47348, "Cantharellus", "genus")
    cibarius = make.taxon(47347, "Cantharellus cibarius", parent=cantharellus)
    make.taxon(99001, "Amanita unphotographed", parent=amanita)
    for taxon in (muscaria, phalloides, cibarius):
        make.image(taxon)
    make.db.execute(
        text("INSERT INTO taxon_names (taxon_id, name, kind, source, language) VALUES "
             "(:a, 'death cap', 'common', 'inaturalist', 'en'), (:b, 'golden chanterelle', 'common', 'inaturalist', 'en'), "
             "(:a, 'Agaricus phalloides', 'synonym', 'inaturalist', NULL)"),
        {"a": phalloides.id, "b": cibarius.id},
    )
    make.db.commit()
    make.refresh()
    return {"amanita": amanita, "muscaria": muscaria, "phalloides": phalloides, "cibarius": cibarius}


def names_in(html: str) -> list[str]:
    return re.findall(r'class="taxon-card__name"><span class="sci"><i>([^<]+)</i>', html)


def test_directory_lists_species_with_photos(client, tree):
    html = client.get("/taxa").text
    assert names_in(html) == ["Amanita muscaria", "Amanita phalloides", "Cantharellus cibarius"]


def test_directory_letter_rank_and_within_filters(client, tree):
    assert names_in(client.get("/taxa?letter=c").text) == ["Cantharellus cibarius"]
    assert names_in(client.get("/taxa?rank=genus").text) == ["Amanita", "Cantharellus"]
    assert names_in(client.get("/taxa?within=48419").text) == ["Amanita muscaria", "Amanita phalloides"]


def test_directory_pagination(client, make):
    genus = make.taxon(1, "Genus", "genus")
    for number in range(50):
        make.image(make.taxon(1000 + number, f"Genus species{number:02d}", parent=genus))
    make.refresh()
    first = client.get("/taxa").text
    second = client.get("/taxa?page=2").text
    assert len(names_in(first)) == 48 and len(names_in(second)) == 2
    assert "Page 1 of 2" in first


def test_search_prefers_exact_then_prefix_matches(client, tree):
    results = client.get("/api/search?q=amanita").json()["results"]
    assert [r["name"] for r in results] == ["Amanita", "Amanita muscaria", "Amanita phalloides"]


def test_search_finds_common_names_and_synonyms(client, tree):
    assert client.get("/api/search?q=death cap").json()["results"][0]["name"] == "Amanita phalloides"
    hit = client.get("/api/search?q=agaricus phalloides").json()["results"][0]
    assert hit["name"] == "Amanita phalloides" and hit["matched"] == "Agaricus phalloides"
    assert client.get("/api/search?q=chanterelle").json()["results"][0]["name"] == "Cantharellus cibarius"


def test_search_tolerates_typos(client, tree):
    names = [r["name"] for r in client.get("/api/search?q=amanita muscari").json()["results"]]
    assert names[0] == "Amanita muscaria"


def test_search_excludes_taxa_without_photos_unless_asked(client, tree):
    assert "Amanita unphotographed" not in str(client.get("/api/search?q=unphotographed").json())
    assert client.get("/api/search?q=unphotographed&scope=all").json()["results"][0]["name"] == "Amanita unphotographed"


def test_search_page_redirects_on_exact_match(client, tree):
    response = client.get("/search?q=Amanita muscaria", follow_redirects=False)
    assert response.headers["location"] == "/taxon/48715/amanita-muscaria"


def test_taxon_urls_use_inat_ids_and_cosmetic_slugs(client, tree):
    assert client.get("/taxon/48715/wrong-slug", follow_redirects=False).headers["location"] == "/taxon/48715/amanita-muscaria"
    assert client.get("/taxon/48715", follow_redirects=False).status_code == 301
    assert client.get("/taxon/99001/amanita-unphotographed").status_code == 404
    page = client.get("/taxon/48715/amanita-muscaria").text
    assert "Fungi" in page and "Amanita</i></span></a>" in page  # breadcrumbs


def test_genus_page_shows_species_and_their_photos(client, tree):
    page = client.get("/taxon/48419/amanita").text
    assert "Amanita phalloides" in page
    assert page.count('<li class="tile') == 2


def test_gallery_pagination_and_fragment(client, db, make):
    species = make.taxon(48715, "Amanita muscaria")
    for number in range(gallery.PAGE_SIZE + 5):
        make.image(species, faves=number)
    make.refresh()
    page = client.get("/taxon/48715/amanita-muscaria").text
    assert page.count('<li class="tile') == gallery.PAGE_SIZE
    assert 'data-next="/taxon/48715/amanita-muscaria/photos?sort=faves&amp;source=all&amp;page=2"' in page
    fragment = client.get("/taxon/48715/amanita-muscaria/photos?sort=faves&page=2").text
    assert fragment.count('<li class="tile') == 5
    assert "<html" not in fragment and "gallery__more" not in fragment


def test_gallery_sorts(db, make):
    species = make.taxon(48715, "Amanita muscaria")
    low, high = make.image(species, faves=1), make.image(species, faves=9)
    assert [i.id for i in gallery.gallery(db, species, sort="faves").images] == [high.id, low.id]
    assert len(gallery.gallery(db, species, sort="random", seed="x").images) == 2
    assert len(gallery.gallery(db, species, sort="community").images) == 2


def test_source_image_ids_are_unique(db, make):
    species = make.taxon(48715, "Amanita muscaria")
    image = make.image(species)
    db.add(Image(**{c: getattr(image, c) for c in (
        "source", "source_image_id", "source_observation_id", "observation_url", "thumb_url",
        "medium_url", "large_url", "photographer", "license_code", "attribution")}))
    with pytest.raises(IntegrityError):
        db.commit()


def test_home_and_static_pages(client, tree):
    assert "Which photos show each fungus best?" in client.get("/").text
    assert client.get("/about").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/nope").status_code == 404
