import datetime
import json
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from common.barcodes import normalize_barcode
from service.config import settings
from service.db.models import ChainStats, StorePrice
from service.db.search import (
    MAX_QUERY_LENGTH,
    CursorExpired,
    directory,
    latest_prices,
    normalize,
    selection,
)
from service.routers.auth import RequireAuth

router = APIRouter(tags=["Products, Chains and Stores"], dependencies=[RequireAuth])
db = settings.get_db()


class ListChainsResponse(BaseModel):
    """List chains response schema."""

    chains: list[str] = Field(..., description="List of retail chain codes.")


@router.get("/chains/", summary="List retail chains")
async def list_chains() -> ListChainsResponse:
    """List all available chains."""
    chains = await directory(db, "chain")
    return ListChainsResponse(chains=[chain["code"] for chain in chains])


class StoreResponse(BaseModel):
    """Store response schema."""

    chain_code: str = Field(..., description="Code of the retail chain.")
    code: str = Field(..., description="Unique code of the store.")
    type: str | None = Field(
        ...,
        description="Type of the store (e.g., supermarket, hypermarket).",
    )
    address: str | None = Field(..., description="Physical address of the store.")
    city: str | None = Field(..., description="City where the store is located.")
    zipcode: str | None = Field(..., description="Postal code of the store location.")
    lat: float | None = Field(..., description="Latitude coordinate of the store.")
    lon: float | None = Field(..., description="Longitude coordinate of the store.")
    phone: str | None = Field(..., description="Phone number of the store.")


class ListStoresResponse(BaseModel):
    """List stores response schema."""

    stores: list[StoreResponse] = Field(
        ..., description="List stores for the specified chain."
    )


@router.get(
    "/{chain_code}/stores/",
    summary="List retail chain stores",
)
async def list_stores(chain_code: str) -> ListStoresResponse:
    """
    List all stores (locations) for a particular chain.

    Future plan: Allow filtering by store type and location.
    """
    rows = await directory(db, "store")
    stores = {r["code"]: r for r in rows if r["chain"] == chain_code}
    if not stores:
        raise HTTPException(404, "No stores for chain")
    return ListStoresResponse(
        stores=[
            StoreResponse(
                chain_code=r["chain"],
                code=r["code"],
                **{
                    k: r[k]
                    for k in (
                        "type",
                        "address",
                        "city",
                        "zipcode",
                        "lat",
                        "lon",
                        "phone",
                    )
                },
            )
            for r in stores.values()
        ]
    )


@router.get("/stores/", summary="Search stores")
async def search_stores(
    chains: str = Query(
        None,
        description="Comma-separated list of chain codes to include, or all",
    ),
    city: str = Query(
        None,
        description="City name for case-insensitive substring match",
    ),
    address: str = Query(
        None,
        description="Address for case-insensitive substring match",
    ),
    lat: float = Query(
        None,
        description="Latitude coordinate for geolocation search",
    ),
    lon: float = Query(
        None,
        description="Longitude coordinate for geolocation search",
    ),
    d: float = Query(
        10.0,
        description="Distance in kilometers for geolocation search (default: 10.0)",
    ),
) -> ListStoresResponse:
    """
    Search for stores by chain codes, city, address, and/or geolocation.

    For geolocation search, both lat and lon must be provided together.
    Note that the geolocation search will only return stores that have
    the geo information available in the database.
    """
    rows = await directory(db, "store")
    try:
        rows = filter_location(rows, chains, city, address, lat, lon, d)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    stores = {(r["chain"], r["code"]): r for r in rows}
    return ListStoresResponse(
        stores=[
            StoreResponse(
                chain_code=r["chain"],
                code=r["code"],
                **{
                    k: r[k]
                    for k in (
                        "type",
                        "address",
                        "city",
                        "zipcode",
                        "lat",
                        "lon",
                        "phone",
                    )
                },
            )
            for r in stores.values()
        ]
    )


def filter_location(rows, chains, city, address, lat, lon, distance):
    import math

    if (lat is None) != (lon is None):
        raise ValueError("Both latitude and longitude are required")
    if distance < 0:
        raise ValueError("Distance must be nonnegative")
    selected = selection(chains)
    result = []
    for row in rows:
        if selected and normalize(row["chain"]) not in selected:
            continue
        if city and normalize(city) not in normalize(row["city"] or ""):
            continue
        if address and normalize(address) not in normalize(row["address"] or ""):
            continue
        if lat is not None:
            if row["lat"] is None or row["lon"] is None:
                continue
            a = math.sin(math.radians(row["lat"] - lat) / 2) ** 2
            a += (
                math.cos(math.radians(lat))
                * math.cos(math.radians(row["lat"]))
                * math.sin(math.radians(row["lon"] - lon) / 2) ** 2
            )
            if 6371 * 2 * math.asin(min(1, math.sqrt(a))) > distance:
                continue
        result.append(row)
    return result


class ChainProductResponse(BaseModel):
    model_config = {"extra": "allow"}

    """Chain product with price information response schema."""

    chain: str = Field(..., description="Chain code.")
    code: str = Field(..., description="Product code within the chain.")
    name: str = Field(..., description="Product name within the chain.")
    brand: str | None = Field(..., description="Product brand within the chain.")
    category: str | None = Field(..., description="Product category within the chain.")
    unit: str | None = Field(..., description="Product unit within the chain.")
    quantity: str | None = Field(..., description="Product quantity within the chain.")
    min_price: Decimal = Field(..., description="Minimum price across chain stores.")
    max_price: Decimal = Field(..., description="Maximum price across chain stores.")
    avg_price: Decimal = Field(..., description="Average price across chain stores.")
    price_date: datetime.date = Field(
        ..., description="Date on which this price was published"
    )


class ProductResponse(BaseModel):
    model_config = {"extra": "allow"}

    """Basic product information response schema."""

    ean: str = Field(..., description="EAN barcode of the product.")
    brand: str | None = Field(..., description="Brand of the product.")
    name: str | None = Field(..., description="Name of the product.")
    quantity: str | None = Field(..., description="Quantity of the product.")
    unit: str | None = Field(..., description="Unit of the product.")
    chains: list[ChainProductResponse] = Field(
        ..., description="List of chain-specific product information."
    )


class ProductSearchResponse(BaseModel):
    model_config = {"extra": "allow"}

    products: list[ProductResponse] = Field(
        ..., description="List of products matching the search query."
    )


@router.get(
    "/products/{ean}/",
    summary="Get product data/prices by barcode",
    response_model=ProductResponse,
)
async def get_product(
    ean: str,
    date: datetime.date = Query(
        None,
        description="Date in YYYY-MM-DD format, defaults to today",
    ),
    chains: str = Query(
        None,
        description="Comma-separated list of chain codes to include",
    ),
    city: list[str] | None = Query(None),
    brand: list[str] | None = Query(None),
    category: list[str] | None = Query(None),
) -> dict:
    """
    Get product information including chain products and prices by their
    barcode. For products that don't have official EAN codes and use
    chain-specific codes, use the "chain:<product_code>" format.

    Prices are the latest known dated observation per shop.
    Explicit historical dates are rejected.
    """

    from service.db.search import search

    if date is not None:
        raise HTTPException(400, "Historical dates are unsupported on latest routes")
    try:
        result = await search(
            db, identity=ean, chains=chains, city=city, brand=brand, category=category
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result["products"]:
        raise HTTPException(404, "Product not found in scope")
    return jsonable_encoder(result["products"][0], custom_encoder={Decimal: str})


class StorePricesResponse(BaseModel):
    store_prices: list[StorePrice] = Field(
        ..., description="For a given product return latest price data per store."
    )


@router.get("/prices/", summary="Get product prices by store with filtering")
async def get_prices(
    eans: str = Query(
        ...,
        description="Comma-separated list of EAN barcodes (required)",
    ),
    chains: str = Query(
        None,
        description="Comma-separated list of chain codes to include",
    ),
    city: str = Query(
        None,
        description="City name for case-insensitive substring match",
    ),
    address: str = Query(
        None,
        description="Address for case-insensitive substring match",
    ),
    lat: float = Query(
        None,
        description="Latitude coordinate for geolocation search",
    ),
    lon: float = Query(
        None,
        description="Longitude coordinate for geolocation search",
    ),
    d: float = Query(
        10.0,
        description="Distance in kilometers for geolocation search (default: 10.0)",
    ),
) -> dict:
    """
    Get product prices by store with store filtering capabilities.

    Returns prices for products in stores matching the filter criteria.
    For geolocation search, both lat and lon must be provided together.
    The EANs parameter is required and must contain at least one EAN code.
    """
    wanted = {
        normalize_barcode(e.strip()) or e.strip() for e in eans.split(",") if e.strip()
    }
    if not wanted:
        raise HTTPException(400, "EANs must be non-empty")
    try:
        rows = await latest_prices(
            db,
            wanted,
            lambda stores: filter_location(stores, chains, city, address, lat, lon, d),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = {
        "store_prices": [
            {
                **{
                    k: r[k]
                    for k in (
                        "ean",
                        "chain",
                        "price_date",
                        "regular_price",
                        "special_price",
                        "unit_price",
                        "best_price_30",
                        "anchor_price",
                    )
                },
                "listing_status": r["listing_status"],
                "store": {
                    "chain_id": r["chain_id"],
                    "code": r["store_code"],
                    **{
                        k: r[k]
                        for k in (
                            "type",
                            "address",
                            "city",
                            "zipcode",
                            "lat",
                            "lon",
                            "phone",
                        )
                    },
                },
            }
            for r in rows
            if r["ean"] in wanted
        ]
    }

    return jsonable_encoder(response, custom_encoder={Decimal: str})


@router.get(
    "/products/",
    summary="Search complete latest product offers",
    response_model=ProductSearchResponse,
)
async def search_products(
    q: str = Query("", max_length=MAX_QUERY_LENGTH),
    date: datetime.date | None = Query(None),
    chains: str | None = Query(None),
    city: list[str] | None = Query(None),
    city_labels: str | None = Query(None, max_length=16384),
    brand: list[str] | None = Query(None),
    category: list[str] | None = Query(None),
    order: str = Query("relevance"),
    fuzzy: bool = Query(False, deprecated=True),
    page_size: int | None = Query(None, ge=1, le=10000),
    limit: int | None = Query(None, ge=1, le=10000, deprecated=True),
    cursor: str | None = Query(None),
) -> dict:
    from service.db.search import search

    if date is not None:
        raise HTTPException(400, "Historical dates are unsupported on latest routes")
    if page_size is not None and limit is not None and page_size != limit:
        raise HTTPException(400, "Conflicting page_size and limit")
    try:
        result = await search(
            db,
            q=q,
            chains=chains,
            city=city,
            city_labels=json.loads(city_labels) if city_labels is not None else None,
            brand=brand,
            category=category,
            order=order,
            page_size=page_size or limit or 100,
            cursor=cursor,
        )
        return jsonable_encoder(result, custom_encoder={Decimal: str})
    except CursorExpired as exc:
        raise HTTPException(409, "cursor_expired") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ChainStatsResponse(BaseModel):
    chain_stats: list[ChainStats] = Field(..., description="List chain stats.")


@router.get("/chain-stats/", summary="Return stats of currently loaded data per chain.")
async def chain_stats() -> dict:
    """Return stats of currently loaded data per chain."""

    from service.db.search import chain_statistics

    rows = await chain_statistics(db)

    return {
        "chain_stats": [
            {
                "chain_code": row["chain"],
                "price_date": row["price_date"],
                "price_count": row["price_count"],
                "store_count": row["store_count"],
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }
            for row in rows
        ]
    }
