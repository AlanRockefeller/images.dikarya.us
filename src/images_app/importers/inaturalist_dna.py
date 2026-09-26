from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from pathlib import Path

import httpx

from images_app.config import settings
from images_app.importers.inaturalist import OBSERVATIONS_API_URL, has_dna_barcode_its

logger = logging.getLogger("images_app.importers.inaturalist_dna")
USER_AGENT = "DikaryaImages/0.1 (https://images.dikarya.us; DNA-verified observation metadata)"
PAGE_SIZE = 200
MAX_REQUESTS = 9000


def export_dna_observation_uuids(
    output_path: str | Path,
    *,
    root_taxon_id: int = settings.fungi_taxon_id,
    client: httpx.Client | None = None,
    request_interval: float = 1.05,
) -> int:
    """Write current fungal observation UUIDs with a non-empty iNat ITS barcode field."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    owns_client = client is None
    http = client or httpx.Client(
        timeout=60,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    last_id = 0
    requests = 0
    observation_count = 0
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow(["observation_uuid"])
            while requests < MAX_REQUESTS:
                params = {
                    "taxon_id": root_taxon_id,
                    "field:DNA Barcode ITS": "",
                    "per_page": PAGE_SIZE,
                    "order_by": "id",
                    "order": "asc",
                    "id_above": last_id,
                }
                response = None
                for attempt in range(6):
                    response = http.get(OBSERVATIONS_API_URL, params=params)
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        break
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = max(float(retry_after), request_interval) if retry_after else min(60.0, 2.0**attempt)
                    except ValueError:
                        delay = min(60.0, 2.0**attempt)
                    logger.warning("iNaturalist API returned %d; retrying in %.1fs", response.status_code, delay)
                    time.sleep(delay)
                if response is None:
                    raise RuntimeError("No response from iNaturalist API")
                response.raise_for_status()
                payload = response.json()
                results = payload.get("results")
                if not isinstance(results, list):
                    raise ValueError("Unexpected iNaturalist observations API response")
                requests += 1
                if not results:
                    break

                page_ids = []
                for observation in results:
                    try:
                        page_ids.append(int(observation["id"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    uuid = str(observation.get("uuid") or "").strip()
                    if uuid and has_dna_barcode_its(observation):
                        writer.writerow([uuid])
                        observation_count += 1
                if not page_ids or max(page_ids) <= last_id:
                    raise ValueError("iNaturalist API pagination made no forward progress")
                last_id = max(page_ids)
                if requests % 100 == 0:
                    stream.flush()
                    logger.info(
                        "scanned %d iNaturalist API pages; staged %d DNA-verified fungal observations",
                        requests,
                        observation_count,
                    )
                if len(results) < PAGE_SIZE:
                    break
                if requests >= MAX_REQUESTS:
                    raise RuntimeError("iNaturalist DNA-observation export reached its request limit")
                time.sleep(request_interval)
            if observation_count == 0:
                raise ValueError("iNaturalist returned no fungal observations with a non-empty ITS barcode field")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        logger.info("exported %d DNA-verified iNaturalist observations", observation_count)
        return observation_count
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            http.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export IDs of iNaturalist fungal observations with ITS barcodes")
    parser.add_argument("--output", required=True)
    parser.add_argument("--root-taxon-id", type=int, default=settings.fungi_taxon_id)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    export_dna_observation_uuids(args.output, root_taxon_id=args.root_taxon_id)


if __name__ == "__main__":
    main()
