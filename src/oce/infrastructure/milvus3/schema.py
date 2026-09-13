"""Milvus collection schemas.

The content collection is keyed by ``chunk_id`` (one content may occur in
several files and positions) and carries ``content_hash`` (the SQL chunk
identity), the chunk text, the dense vector, ``blob_name`` for scope
filtering and a JSON metadata field.
"""

from pymilvus import CollectionSchema, DataType, FieldSchema


def create_oce_collection_schema(dense_dim: int = 1024) -> CollectionSchema:
    """The content collection schema for ``dense_dim`` vectors."""
    fields = [
        FieldSchema(
            name="chunk_id",
            dtype=DataType.VARCHAR,
            max_length=64,
            is_primary=True,
            description="Stable hash of blob, content and position",
        ),
        FieldSchema(
            name="content_hash",
            dtype=DataType.VARCHAR,
            max_length=64,
            description="SHA256 of the content alone",
        ),
        FieldSchema(
            name="content",
            dtype=DataType.VARCHAR,
            max_length=65535,
            description="Chunk text",
        ),
        FieldSchema(
            name="dense_vector",
            dtype=DataType.FLOAT_VECTOR,
            dim=dense_dim,
            description="Dense embedding",
        ),
        FieldSchema(
            name="blob_name",
            dtype=DataType.VARCHAR,
            max_length=64,
            description="Blob name for scope filtering",
        ),
        FieldSchema(
            name="metadata",
            dtype=DataType.JSON,
            description="Metadata (path, start_line, end_line)",
        ),
    ]

    schema = CollectionSchema(
        fields=fields,
        description="OCE chunk vectors",
        enable_dynamic_field=False,
    )

    return schema


def create_path_collection_schema(dense_dim: int = 1024) -> CollectionSchema:
    """Create the schema for semantic filename and path retrieval."""
    return CollectionSchema(
        fields=[
            FieldSchema(
                name="path_id",
                dtype=DataType.VARCHAR,
                max_length=512,
                is_primary=True,
                description="path_{blob_name}",
            ),
            FieldSchema(
                name="blob_name",
                dtype=DataType.VARCHAR,
                max_length=64,
                description="Blob identifier for filtering",
            ),
            FieldSchema(
                name="path",
                dtype=DataType.VARCHAR,
                max_length=512,
                description="Original file path",
            ),
            FieldSchema(
                name="path_document",
                dtype=DataType.VARCHAR,
                max_length=2048,
                description="Semantic path document",
            ),
            FieldSchema(
                name="path_vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=dense_dim,
                description="Path document embedding",
            ),
        ],
        description="Path-only index for filename queries",
        enable_dynamic_field=False,
    )
