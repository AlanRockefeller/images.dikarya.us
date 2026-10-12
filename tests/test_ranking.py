import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from images_app.models import Image, Taxon, Vote

from .conftest import csrf, login


@pytest.fixture
def setup(make):
    genus = make.taxon(48419, "Amanita", "genus")
    species = make.taxon(48715, "Amanita muscaria", parent=genus)
    images = [make.image(species, faves=10 - i) for i in range(5)]
    make.user()
    make.refresh()
    return species, images


def save(client, image_ids, taxon_id=48715):
    return client.put(
        f"/api/taxa/{taxon_id}/ranking",
        json={"image_ids": image_ids},
        headers={"X-CSRF-Token": csrf(client)},
    )


def stored(db, user_id=1, taxon_id=None):
    query = select(Vote.image_id, Vote.rank).where(Vote.user_id == user_id).order_by(Vote.rank)
    if taxon_id:
        query = query.where(Vote.taxon_id == taxon_id)
    return [tuple(row) for row in db.execute(query).all()]


def test_voting_requires_login(client, setup):
    _, images = setup
    response = save(client, [images[0].id])
    assert response.status_code == 401
    assert response.json() == {"error": "Please log in first."}


def test_voting_requires_csrf_token(client, setup):
    _, images = setup
    login(client)
    response = client.put("/api/taxa/48715/ranking", json={"image_ids": [images[0].id]})
    assert response.status_code == 403


def test_first_selection_is_rank_one_and_next_is_rank_two(client, db, setup):
    _, images = setup
    login(client)
    assert save(client, [images[2].id]).json() == {"image_ids": [images[2].id]}
    assert stored(db) == [(images[2].id, 1)]
    save(client, [images[2].id, images[0].id])
    assert stored(db) == [(images[2].id, 1), (images[0].id, 2)]


def test_deselecting_closes_the_gap(client, db, setup):
    _, images = setup
    login(client)
    save(client, [i.id for i in images[:4]])
    save(client, [images[0].id, images[2].id, images[3].id])
    assert stored(db) == [(images[0].id, 1), (images[2].id, 2), (images[3].id, 3)]


def test_reordering(client, db, setup):
    _, images = setup
    login(client)
    save(client, [images[0].id, images[1].id, images[2].id])
    first_created = db.scalar(select(Vote.created_at).where(Vote.image_id == images[0].id))
    save(client, [images[2].id, images[0].id, images[1].id])
    assert stored(db) == [(images[2].id, 1), (images[0].id, 2), (images[1].id, 3)]
    # Moving a vote keeps its history.
    assert db.scalar(select(Vote.created_at).where(Vote.image_id == images[0].id)) == first_created


def test_duplicates_and_foreign_images_are_ignored(client, db, setup, make):
    _, images = setup
    other = make.taxon(99999, "Russula emetica")
    stranger = make.image(other)
    login(client)
    response = save(client, [images[1].id, images[1].id, stranger.id, images[0].id])
    assert response.json() == {"image_ids": [images[1].id, images[0].id]}


def test_hidden_images_cannot_be_added(client, setup, make):
    species, _ = setup
    hidden = make.image(species, active=False)
    login(client)
    assert save(client, [hidden.id]).json() == {"image_ids": []}


def test_database_rejects_duplicate_rank_and_duplicate_image(db, setup):
    species, images = setup
    db.add_all([Vote(user_id=1, taxon_id=species.id, image_id=images[0].id, rank=1),
                Vote(user_id=1, taxon_id=species.id, image_id=images[1].id, rank=1)])
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add_all([Vote(user_id=1, taxon_id=species.id, image_id=images[0].id, rank=1),
                Vote(user_id=1, taxon_id=species.id, image_id=images[0].id, rank=2)])
    with pytest.raises(IntegrityError):
        db.commit()


def test_two_users_rank_independently(client, db, setup, make):
    _, images = setup
    make.user(email="second@example.org")
    login(client)
    save(client, [images[0].id, images[1].id])
    client.cookies.clear()
    login(client, email="second@example.org")
    save(client, [images[1].id])
    assert stored(db, user_id=1) == [(images[0].id, 1), (images[1].id, 2)]
    assert stored(db, user_id=2) == [(images[1].id, 1)]


def test_rankings_persist_across_sessions(client, setup):
    _, images = setup
    login(client)
    save(client, [images[3].id, images[1].id])
    client.cookies.clear()
    login(client)
    page = client.get("/taxon/48715/amanita-muscaria").text
    assert f'<li class="tray__item" data-id="{images[3].id}"' in page
    assert 'class="tile is-ranked" id="photo-%d"' % images[1].id in page


def test_hidden_votes_are_kept_and_moved_behind_visible_ones(client, db, setup, make):
    _, images = setup
    login(client)
    save(client, [images[0].id, images[1].id, images[2].id])
    db.execute(update(Image).where(Image.id == images[0].id).values(is_active=False, inactive_reason="missing_upstream"))
    db.commit()
    make.refresh()
    assert stored(db) == [(images[1].id, 1), (images[2].id, 2), (images[0].id, 3)]
    # The user sees ranks 1 and 2; saving again keeps the hidden vote at the end.
    save(client, [images[2].id, images[1].id, images[4].id])
    assert stored(db) == [(images[2].id, 1), (images[1].id, 2), (images[4].id, 3), (images[0].id, 4)]


def test_reidentified_image_votes_stay_with_original_taxon(client, db, setup, make):
    species, images = setup
    login(client)
    save(client, [images[0].id, images[1].id])
    other = make.taxon(55555, "Amanita pantherina")
    db.execute(update(Image).where(Image.id == images[0].id).values(taxon_id=other.id))
    db.commit()
    make.refresh()
    assert stored(db) == [(images[1].id, 1), (images[0].id, 2)]
    assert db.scalar(select(Vote.taxon_id).where(Vote.image_id == images[0].id)) == species.id


def test_species_gallery_includes_varieties(client, db, setup, make):
    species, images = setup
    variety = make.taxon(321524, "Amanita muscaria guessowii", "variety", parent=species)
    var_image = make.image(variety)
    make.refresh()
    login(client)
    assert save(client, [var_image.id, images[0].id]).json()["image_ids"] == [var_image.id, images[0].id]
    page = client.get("/taxon/48715/amanita-muscaria").text
    assert f'id="photo-{var_image.id}"' in page
    assert "Amanita muscaria</i> var. <i>guessowii" in page
    exact = client.get("/taxon/48715/amanita-muscaria?only=1").text
    assert f'id="photo-{var_image.id}"' not in exact


def test_no_javascript_toggle_form(client, db, setup):
    _, images = setup
    login(client)
    for image in (images[0], images[1], images[0]):
        response = client.post(
            "/taxon/48715/toggle",
            data={"image_id": image.id, "csrf_token": csrf(client), "back": "/taxon/48715/amanita-muscaria"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert stored(db) == [(images[1].id, 1)]


def test_community_results(client, db, setup, make):
    _, images = setup
    make.user(email="second@example.org")
    login(client)
    save(client, [images[0].id, images[1].id])
    client.cookies.clear()
    login(client, email="second@example.org")
    save(client, [images[0].id, images[2].id])
    page = client.get("/taxon/48715/amanita-muscaria/results").text
    assert "2 people have ranked" in page
    rows = page.split("<tbody>")[1].split("<tr>")[1:]
    assert str(images[0].id) in rows[0] and "<td>2</td>" in rows[0]  # two first places


def test_cover_follows_first_place_votes(client, db, setup):
    species, images = setup
    db.refresh(species)
    assert species.cover_image_id == images[0].id  # most faved
    login(client)
    save(client, [images[4].id])
    db.expire_all()
    assert db.get(Taxon, species.id).cover_image_id == images[4].id


def test_genus_cover_reverts_when_its_last_vote_is_removed(client, db, setup):
    species, images = setup
    genus = db.get(Taxon, species.parent_id)
    assert genus.cover_image_id == images[0].id  # borrowed from the species
    login(client)
    save(client, [images[4].id], taxon_id=48419)
    db.expire_all()
    assert db.get(Taxon, genus.id).cover_image_id == images[4].id
    save(client, [], taxon_id=48419)
    db.expire_all()
    assert db.get(Taxon, genus.id).cover_image_id == images[0].id
