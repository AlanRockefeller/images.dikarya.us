import csv
import io
import zipfile

import pytest
from sqlalchemy import select, text

from images_app.models import Taxon, TaxonName
from images_app.sync import taxonomy
from images_app.sync.runs import tracked_run

from .conftest import migrate, reset_schema

URI = "https://www.inaturalist.org/taxa/"


def archive(tmp_path, rows, common=()):
    """A minimal iNaturalist DwC-A: (id, parent, name, rank) tuples."""
    taxa = io.StringIO()
    writer = csv.writer(taxa)
    writer.writerow(["id", "taxonID", "parentNameUsageID", "kingdom", "scientificName", "taxonRank"])
    for taxon_id, parent, name, rank in rows:
        writer.writerow([taxon_id, URI + str(taxon_id), URI + str(parent) if parent else "", "", name, rank])
    names = io.StringIO()
    writer = csv.writer(names)
    writer.writerow(["id", "vernacularName", "language"])
    writer.writerows([(taxon_id, name, "en") for taxon_id, name in common])
    path = tmp_path / "taxonomy.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("taxa.csv", taxa.getvalue())
        bundle.writestr("VernacularNames-english.csv", names.getvalue())
    return path


TREE = [
    (48460, None, "Life", "stateofmatter"),
    (1, 48460, "Animalia", "kingdom"),
    (47170, 48460, "Fungi", "kingdom"),
    (48419, 47170, "Amanita", "genus"),
    (48715, 48419, "Amanita muscaria", "species"),
    (321524, 48715, "Amanita muscaria guessowii", "variety"),
    (52, 1, "Felis catus", "species"),
]


def run_import(path):
    with tracked_run("taxonomy") as run:
        taxonomy.import_taxonomy(run, path)
    return run


def test_imports_only_descendants_of_root_with_ancestry(db, tmp_path):
    run_import(archive(tmp_path, TREE, common=[(48715, "Fly Agaric"), (48715, "fly amanita"), (52, "Cat")]))
    taxa = {t.inat_taxon_id: t for t in db.scalars(select(Taxon)).all()}
    assert set(taxa) == {47170, 48419, 48715, 321524}
    assert taxa[321524].ancestor_ids == [47170, 48419, 48715]
    assert taxa[321524].parent_id == taxa[48715].id
    assert (taxa[321524].rank, taxa[321524].rank_level) == ("variety", 5)
    assert taxa[48715].common_name == "Fly Agaric"
    names = set(db.scalars(select(TaxonName.name).where(TaxonName.taxon_id == taxa[48715].id)).all())
    assert names == {"Amanita muscaria", "Fly Agaric", "fly amanita"}


def test_reimport_is_idempotent_and_tracks_renames_and_removals(db, tmp_path):
    run_import(archive(tmp_path, TREE))
    ids = dict(db.execute(select(Taxon.inat_taxon_id, Taxon.id)).all())
    renamed = [row for row in TREE if row[0] != 321524]
    renamed = [(i, p, "Amanita muscarius" if i == 48715 else n, r) for i, p, n, r in renamed]
    # Pad the archive so the "truncated archive" guard is not triggered.
    renamed += [(900000 + n, 48419, f"Amanita filler{n}", "species") for n in range(5)]
    run_import(archive(tmp_path, renamed))
    db.expire_all()
    assert dict(db.execute(select(Taxon.inat_taxon_id, Taxon.id).where(Taxon.inat_taxon_id.in_(ids))).all()) == ids
    assert db.scalar(select(Taxon.is_active).where(Taxon.inat_taxon_id == 321524)) is False
    muscaria = db.scalar(select(Taxon).where(Taxon.inat_taxon_id == 48715))
    assert muscaria.name == "Amanita muscarius"
    kinds = dict(db.execute(select(TaxonName.name, TaxonName.kind).where(TaxonName.taxon_id == muscaria.id)).all())
    assert kinds == {"Amanita muscarius": "scientific", "Amanita muscaria": "synonym"}


def test_refuses_to_deactivate_most_taxa_from_a_truncated_archive(db, tmp_path):
    run_import(archive(tmp_path, TREE))
    with pytest.raises(ValueError, match="truncated"):
        run_import(archive(tmp_path, TREE[:4]))


def test_migration_moves_legacy_tables_and_keeps_accounts(db):
    reset_schema()
    with db.bind.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('ab38d1e0f5d7')"))
        connection.execute(text(
            "CREATE TABLE users (id bigserial PRIMARY KEY, name varchar(120), email varchar(320), password_hash varchar(255), "
            "created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), is_admin boolean, is_active boolean)"
        ))
        connection.execute(text("CREATE TABLE taxa (id bigserial PRIMARY KEY, parent_id bigint)"))
        connection.execute(text("CREATE INDEX ix_taxa_parent_id ON taxa (parent_id)"))  # same name as the new schema's
        connection.execute(text(
            "INSERT INTO users (id, name, email, password_hash, is_admin, is_active) "
            "VALUES (7, 'Alan', 'Alan@Example.org', '$argon2id$v=19$x', true, true)"
        ))
        connection.execute(text("CREATE TABLE images (id bigserial PRIMARY KEY)"))
    migrate()
    with db.bind.connect() as connection:
        assert connection.execute(text("SELECT id, email, is_admin FROM users")).one() == (7, "alan@example.org", True)
        assert connection.execute(text("SELECT count(*) FROM legacy_images")).scalar() == 0
        assert connection.execute(text("SELECT version_num FROM legacy_alembic_version")).scalar() == "ab38d1e0f5d7"
        assert connection.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'legacy_ix_taxa_parent_id'")).scalar() == 1
        assert connection.execute(text("SELECT nextval('users_id_seq')")).scalar() == 8
