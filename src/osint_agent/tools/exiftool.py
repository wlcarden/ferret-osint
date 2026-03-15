"""ExifTool adapter — image/media metadata extraction."""

import json

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter


class ExifToolAdapter(ToolAdapter):
    """Wraps ExifTool CLI for extracting metadata from images and media files.

    Extracts GPS coordinates, camera info, timestamps, software used,
    and other embedded metadata. Particularly useful for geolocation
    and timeline reconstruction.
    """

    name = "exiftool"
    required_binary = "exiftool"
    install_hint = "apt install libimage-exiftool-perl"

    async def run(self, file_path: str) -> Finding:
        """Extract metadata from an image or media file.

        Returns a Finding containing:
        - A DOCUMENT entity with all extracted metadata as properties
        - An ADDRESS entity if GPS coordinates are found
        """
        result = await self.run_subprocess(
            ["exiftool", "-json", "-G", "-n", file_path],
            timeout=30,
        )

        if result.returncode != 0:
            return Finding(notes=f"ExifTool failed on '{file_path}': {result.stderr[:500]}")

        try:
            metadata_list = json.loads(result.stdout)
        except json.JSONDecodeError:
            return Finding(notes=f"ExifTool returned unparseable output for '{file_path}'")

        if not metadata_list:
            return Finding(notes=f"ExifTool: no metadata extracted from '{file_path}'")

        return self._parse_metadata(file_path, metadata_list[0])

    def _parse_metadata(self, file_path: str, metadata: dict) -> Finding:
        """Parse ExifTool JSON output into entities."""
        entities = []
        relationships = []

        # Flatten the grouped keys (ExifTool -G prefixes with group name)
        flat = {}
        for key, value in metadata.items():
            # Keys come as "EXIF:GPSLatitude", "File:FileName", etc.
            flat[key] = value

        doc_id = f"document:exif:{file_path}"
        # Extract key fields
        properties = {
            "file_path": file_path,
            "file_name": flat.get("File:FileName", ""),
            "file_type": flat.get("File:FileType", ""),
            "file_size": flat.get("File:FileSize", ""),
            "mime_type": flat.get("File:MIMEType", ""),
            "image_width": flat.get("File:ImageWidth", flat.get("EXIF:ImageWidth")),
            "image_height": flat.get("File:ImageHeight", flat.get("EXIF:ImageHeight")),
            "camera_make": flat.get("EXIF:Make", ""),
            "camera_model": flat.get("EXIF:Model", ""),
            "software": flat.get("EXIF:Software", ""),
            "create_date": flat.get("EXIF:CreateDate", flat.get("EXIF:DateTimeOriginal", "")),
            "modify_date": flat.get("EXIF:ModifyDate", ""),
            "source_system": "exiftool",
        }
        # Remove None/empty values
        properties = {k: v for k, v in properties.items() if v}

        doc = Entity(
            id=doc_id,
            entity_type=EntityType.DOCUMENT,
            label=f"Image: {flat.get('File:FileName', file_path)}",
            properties=properties,
            sources=[Source(tool=self.name, raw_data=metadata)],
        )
        entities.append(doc)

        # Extract GPS coordinates if present
        lat = flat.get("EXIF:GPSLatitude") or flat.get("Composite:GPSLatitude")
        lon = flat.get("EXIF:GPSLongitude") or flat.get("Composite:GPSLongitude")

        if lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                addr_id = f"address:gps:{lat_f:.6f},{lon_f:.6f}"
                entities.append(Entity(
                    id=addr_id,
                    entity_type=EntityType.ADDRESS,
                    label=f"GPS: {lat_f:.6f}, {lon_f:.6f}",
                    properties={
                        "latitude": lat_f,
                        "longitude": lon_f,
                        "altitude": flat.get("EXIF:GPSAltitude"),
                        "source_system": "exiftool",
                    },
                    sources=[Source(tool=self.name)],
                ))
                relationships.append(Relationship(
                    source_id=doc_id,
                    target_id=addr_id,
                    relation_type=RelationType.HAS_ADDRESS,
                    properties={"address_type": "gps_coordinates"},
                    sources=[Source(tool=self.name)],
                ))
            except (ValueError, TypeError):
                pass

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"ExifTool: extracted {len(properties)} properties from '{file_path}'"
            + (f", GPS: {lat}, {lon}" if lat and lon else ", no GPS data"),
        )
