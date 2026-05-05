# -*- coding: utf-8 -*-
"""
/***************************************************************************
 DEMto3D
                                 A QGIS plugin
 Description
                             -------------------
        copyright            : (C) 2022 by Javier
        email                : demto3d@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import collections
import math
import zipfile

from qgis.core import QgsCoordinateTransform, QgsProject, QgsWkbTypes
from qgis.PyQt.QtCore import QThread, pyqtSignal

# Trail export modes
TRAIL_MODE_RAISED = "raised"
TRAIL_MODE_ENGRAVED = "engraved"
TRAIL_MODE_SEPARATE = "separate"

# Default trail geometry parameters (mm)
TRAIL_DEFAULT_WIDTH = 1.0
TRAIL_DEFAULT_HEIGHT = 0.8
TRAIL_DEFAULT_DEPTH = 0.4


class ThreeMF(QThread):
    """Writes a .3mf file from the mesh point matrix that describes the model surface.

    The 3MF format is a ZIP-based XML format understood by all major slicers
    (PrusaSlicer, OrcaSlicer, Bambu Studio, etc.).  When a trail layer is
    provided and trail_mode is TRAIL_MODE_SEPARATE, the terrain and trail
    are exported as *two separate objects* inside the same 3MF archive so
    that multicolor / multi-material slicers (e.g., those supporting AMS
    systems) can assign different colours/extruders to each body.
    """

    pto = collections.namedtuple('pto', 'x y z')
    normal = collections.namedtuple('normal', 'normal_x normal_y normal_z')
    updateProgress = pyqtSignal()

    # 3MF archive entry names
    _CONTENT_TYPES = "[Content_Types].xml"
    _RELS_DIR = "_rels"
    _RELS_FILE = "_rels/.rels"
    _MODEL_FILE = "3D/3dmodel.model"

    # 3MF XML namespaces
    _NS_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
    _NS_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
    _NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    _REL_TYPE_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

    def __init__(self, parameters, output_file, dem_matrix, trail_layer=None):
        """
        Parameters
        ----------
        parameters : dict
            Same parameters dict used by STL_Builder.  May additionally contain:
            ``trail_mode``  – one of TRAIL_MODE_RAISED / TRAIL_MODE_ENGRAVED /
                               TRAIL_MODE_SEPARATE (default: TRAIL_MODE_RAISED).
            ``trail_width`` – ribbon half-width in mm (default 1.0).
            ``trail_height``– ribbon height above terrain in mm (default 0.8).
            ``trail_depth`` – engraved depth below terrain in mm (default 0.4).
        output_file : str
            Path of the output .3mf file.
        dem_matrix : list[list[namedtuple]]
            2-D array of (x, y, z) namedtuples in model-mm coordinates,
            as produced by Model_Builder.
        trail_layer : QgsVectorLayer or None
            Optional line vector layer whose features define the trail path.
            Each feature's geometry is projected onto the terrain surface and
            exported as an additional object in the 3MF archive.
        """
        QThread.__init__(self)
        self.parameters = parameters
        self.output_file = output_file
        self.matrix_dem = dem_matrix
        self.trail_layer = trail_layer
        self.quit = False

    # ------------------------------------------------------------------
    # QThread entry-point
    # ------------------------------------------------------------------

    def run(self):
        x_models = self.parameters["divideCols"]
        y_models = self.parameters["divideRow"]

        width_model = self.parameters["width"] / x_models
        high_model = self.parameters["height"] / y_models

        # Collect all terrain objects (one per tile)
        objects_xml = []
        object_id = 1

        for i in range(y_models):
            for j in range(x_models):
                x_min_model = width_model * j
                y_min_model = self.parameters["height"] - i * high_model - high_model
                x_max_model = width_model * j + width_model
                y_max_model = self.parameters["height"] - i * high_model

                dem_model = self._cut_dem(
                    self.matrix_dem,
                    self.parameters["spacing_mm"],
                    x_min_model,
                    y_min_model,
                    x_max_model,
                    y_max_model,
                )
                if self.quit:
                    return

                tile_name = "Terrain"
                if y_models * x_models > 1:
                    tile_name = "Terrain_{}_{}".format(i, j)

                vertices, triangles = self._build_terrain_mesh(dem_model)
                self.updateProgress.emit()

                if self.quit:
                    return

                objects_xml.append(
                    self._object_xml(object_id, tile_name, vertices, triangles)
                )
                object_id += 1

        # Optional trail objects (built from the trail vector layer)
        trail_mode = self.parameters.get("trail_mode", TRAIL_MODE_RAISED)
        if self.trail_layer is not None:
            trail_width = self.parameters.get("trail_width", TRAIL_DEFAULT_WIDTH)
            trail_height = self.parameters.get("trail_height", TRAIL_DEFAULT_HEIGHT)
            trail_depth = self.parameters.get("trail_depth", TRAIL_DEFAULT_DEPTH)
            trail_polylines = self._process_trail_layer(self.trail_layer)
            for idx, polyline in enumerate(trail_polylines):
                if self.quit:
                    return
                vertices, triangles = self._build_trail_mesh(
                    polyline, trail_mode, trail_width, trail_height, trail_depth
                )
                if vertices:
                    objects_xml.append(
                        self._object_xml(
                            object_id,
                            "Trail_{}".format(idx + 1),
                            vertices,
                            triangles,
                        )
                    )
                    object_id += 1

        # Write the archive
        self._write_3mf(objects_xml)

    # ------------------------------------------------------------------
    # Terrain mesh helpers
    # ------------------------------------------------------------------

    def _build_terrain_mesh(self, matrix_dem):
        """Return (vertices, triangles) for the full terrain solid.

        The solid has:
        * a top surface  (the terrain elevation)
        * a flat bottom  (z = 0)
        * four side walls
        """
        rows = len(matrix_dem)
        cols = len(matrix_dem[0])

        vertices = []
        vert_idx = {}  # (i, j) -> vertex index

        def _add(pt):
            key = (round(pt.x, 4), round(pt.y, 4), round(pt.z, 4))
            if key not in vert_idx:
                vert_idx[key] = len(vertices)
                vertices.append(pt)
            return vert_idx[key]

        triangles = []

        # --- top surface ---
        top_idx = [[None] * cols for _ in range(rows)]
        for i in range(rows):
            for j in range(cols):
                top_idx[i][j] = _add(matrix_dem[i][j])

        for i in range(rows - 1):
            for j in range(cols - 1):
                p1 = top_idx[i + 1][j]
                p2 = top_idx[i][j + 1]
                p3 = top_idx[i][j]
                p4 = top_idx[i + 1][j + 1]
                triangles.append((p1, p2, p3))
                triangles.append((p1, p4, p2))

        # --- bottom face (z = 0) ---
        bot_idx = [[None] * cols for _ in range(rows)]
        for i in range(rows):
            for j in range(cols):
                pt = matrix_dem[i][j]._replace(z=0)
                bot_idx[i][j] = _add(pt)

        for i in range(rows - 1):
            for j in range(cols - 1):
                p1 = bot_idx[i + 1][j]
                p2 = bot_idx[i][j + 1]
                p3 = bot_idx[i][j]
                p4 = bot_idx[i + 1][j + 1]
                # Reversed winding for downward-facing normal
                triangles.append((p1, p3, p2))
                triangles.append((p1, p2, p4))

        # --- side walls ---
        # Left wall (j = 0)
        for i in range(rows - 1):
            t = top_idx[i][0]
            t2 = top_idx[i + 1][0]
            b = bot_idx[i][0]
            b2 = bot_idx[i + 1][0]
            triangles.append((t, b, t2))
            triangles.append((b, b2, t2))

        # Right wall (j = cols-1)
        for i in range(rows - 1):
            t = top_idx[i][cols - 1]
            t2 = top_idx[i + 1][cols - 1]
            b = bot_idx[i][cols - 1]
            b2 = bot_idx[i + 1][cols - 1]
            triangles.append((t, t2, b))
            triangles.append((b, t2, b2))

        # Front wall (i = rows-1)
        for j in range(cols - 1):
            t = top_idx[rows - 1][j]
            t2 = top_idx[rows - 1][j + 1]
            b = bot_idx[rows - 1][j]
            b2 = bot_idx[rows - 1][j + 1]
            triangles.append((t, t2, b))
            triangles.append((b, t2, b2))

        # Back wall (i = 0)
        for j in range(cols - 1):
            t = top_idx[0][j]
            t2 = top_idx[0][j + 1]
            b = bot_idx[0][j]
            b2 = bot_idx[0][j + 1]
            triangles.append((t, b, t2))
            triangles.append((b, b2, t2))

        return vertices, triangles

    # ------------------------------------------------------------------
    # Trail mesh helpers
    # ------------------------------------------------------------------

    def _build_trail_mesh(self, polyline, mode, width, height, depth):
        """Build a closed trail ribbon mesh.

        Parameters
        ----------
        polyline : list[pto]
            Ordered list of (x, y, z) points along the trail in model-mm
            coordinates.  z should already reflect the terrain elevation at
            that point.
        mode : str
            One of TRAIL_MODE_RAISED, TRAIL_MODE_ENGRAVED, TRAIL_MODE_SEPARATE.
        width : float
            Half-width of the trail ribbon (mm).  The ribbon extends width/2
            to each side of the centerline.
        height : float
            Height of the ribbon above the terrain surface (used for RAISED
            and SEPARATE modes).
        depth : float
            Depth of the ribbon below the terrain surface (used for ENGRAVED
            mode).

        Returns
        -------
        vertices, triangles : lists
        """
        if len(polyline) < 2:
            return [], []

        half_width = width / 2.0

        if mode == TRAIL_MODE_ENGRAVED:
            z_top_offset = 0.0
            z_bot_offset = -depth
        else:
            # RAISED or SEPARATE: ribbon sits on or above the terrain surface
            z_top_offset = height
            z_bot_offset = 0.0
            # RAISED or SEPARATE: ribbon sits on or above the terrain surface
            z_top_offset = height
            z_bot_offset = 0.0

        vertices = []
        triangles = []
        vert_idx = {}

        def _add(pt):
            key = (round(pt.x, 4), round(pt.y, 4), round(pt.z, 4))
            if key not in vert_idx:
                vert_idx[key] = len(vertices)
                vertices.append(pt)
            return vert_idx[key]

        def _perp(dx, dy):
            """Unit perpendicular vector (rotated 90° CCW)."""
            length = math.sqrt(dx * dx + dy * dy)
            if length < 1e-9:
                return 0.0, 0.0
            return -dy / length, dx / length

        # Build four corner strips: top-left, top-right, bot-left, bot-right
        top_left = []
        top_right = []
        bot_left = []
        bot_right = []

        for k, pt in enumerate(polyline):
            if k == 0:
                dx = polyline[1].x - pt.x
                dy = polyline[1].y - pt.y
            elif k == len(polyline) - 1:
                dx = pt.x - polyline[k - 1].x
                dy = pt.y - polyline[k - 1].y
            else:
                dx = polyline[k + 1].x - polyline[k - 1].x
                dy = polyline[k + 1].y - polyline[k - 1].y

            px, py = _perp(dx, dy)

            tl = self.pto(x=pt.x + px * half_width, y=pt.y + py * half_width, z=pt.z + z_top_offset)
            tr = self.pto(x=pt.x - px * half_width, y=pt.y - py * half_width, z=pt.z + z_top_offset)
            bl = self.pto(x=pt.x + px * half_width, y=pt.y + py * half_width, z=pt.z + z_bot_offset)
            br = self.pto(x=pt.x - px * half_width, y=pt.y - py * half_width, z=pt.z + z_bot_offset)

            top_left.append(_add(tl))
            top_right.append(_add(tr))
            bot_left.append(_add(bl))
            bot_right.append(_add(br))

        n = len(polyline)
        for k in range(n - 1):
            tl0, tl1 = top_left[k], top_left[k + 1]
            tr0, tr1 = top_right[k], top_right[k + 1]
            bl0, bl1 = bot_left[k], bot_left[k + 1]
            br0, br1 = bot_right[k], bot_right[k + 1]

            # Top face
            triangles.append((tl0, tr0, tl1))
            triangles.append((tr0, tr1, tl1))
            # Bottom face (reversed)
            triangles.append((bl0, bl1, br0))
            triangles.append((br0, bl1, br1))
            # Left side
            triangles.append((bl0, tl0, tl1))
            triangles.append((bl0, tl1, bl1))
            # Right side
            triangles.append((tr0, br0, tr1))
            triangles.append((br0, br1, tr1))

        # Cap at start
        triangles.append((top_left[0], bot_left[0], top_right[0]))
        triangles.append((bot_left[0], bot_right[0], top_right[0]))
        # Cap at end
        k = n - 1
        triangles.append((top_left[k], top_right[k], bot_left[k]))
        triangles.append((bot_left[k], top_right[k], bot_right[k]))

        return vertices, triangles

    # ------------------------------------------------------------------
    # Trail layer processing helpers
    # ------------------------------------------------------------------

    def _process_trail_layer(self, trail_layer):
        """Convert features in ``trail_layer`` to model-mm polylines.

        Each feature's geometry is densified to ``spacing_mm`` resolution,
        reprojected to the map CRS, converted to model-space mm coordinates,
        and the z value at each point is sampled from ``self.matrix_dem``.

        Parameters
        ----------
        trail_layer : QgsVectorLayer
            A line or multiline vector layer.

        Returns
        -------
        list[list[pto]]
            One polyline (list of pto namedtuples) per feature/part.
        """
        if trail_layer is None:
            return []

        params = self.parameters
        spacing_mm = params["spacing_mm"]
        height = params["height"]
        width = params["width"]
        crs_map = params["crs_map"]
        crs_layer_trail = trail_layer.crs()
        rect_param = params.get("roi_rect_Param") or {}
        rotation = rect_param.get("rotation", 0.0)
        projected = params.get("projected", True)

        # Scale factors for geographic->mm conversion
        if projected:
            scale = params["scale"]
            # 1 geographic unit (m) = 1000/scale mm
            geo_to_mm = 1000.0 / scale
        else:
            # Non-projected (degree) CRS
            rect_width_geo = rect_param.get("width", 1.0)
            spacing_deg = spacing_mm * rect_width_geo / width
            geo_to_mm = spacing_mm / spacing_deg if spacing_deg else 1.0

        roi_x_min = params["roi_x_min"]
        roi_y_min = params["roi_y_min"]

        # Coordinate transform: trail layer CRS -> map CRS
        transform = None
        if crs_layer_trail != crs_map:
            transform = QgsCoordinateTransform(
                crs_layer_trail, crs_map, QgsProject.instance()
            )

        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)

        def _geo_to_mm(x_geo, y_geo):
            """Transform a geographic map-CRS coordinate to model mm."""
            dx = x_geo - roi_x_min
            dy = y_geo - roi_y_min
            # Inverse rotation
            x_mm = (dx * cos_r + dy * sin_r) * geo_to_mm
            y_mm = (-dx * sin_r + dy * cos_r) * geo_to_mm
            return x_mm, y_mm

        polylines = []

        for feature in trail_layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue

            # Densify to ensure adequate resolution along the trail
            geom = geom.densifyByDistance(spacing_mm / geo_to_mm)

            # Collect all parts (handles both single and multi-part)
            wkb_type = geom.wkbType()
            if QgsWkbTypes.isMultiType(wkb_type):
                parts = [geom.asMultiPolyline()]
                parts = [p for sublist in parts for p in sublist]
            else:
                parts = [geom.asPolyline()]

            for part in parts:
                pts = []
                for qgs_pt in part:
                    x, y = qgs_pt.x(), qgs_pt.y()
                    # Reproject to map CRS if needed
                    if transform is not None:
                        p = transform.transform(x, y)
                        x, y = p.x(), p.y()
                    x_mm, y_mm = _geo_to_mm(x, y)
                    z_mm = self._sample_z_from_matrix(x_mm, y_mm)
                    pts.append(self.pto(x=x_mm, y=y_mm, z=z_mm))

                if len(pts) >= 2:
                    polylines.append(pts)

        return polylines

    def _sample_z_from_matrix(self, x_mm, y_mm):
        """Bilinear-interpolate the terrain elevation (mm) at model coords.

        Parameters
        ----------
        x_mm, y_mm : float
            Position in model-space millimetres.

        Returns
        -------
        float
            Interpolated z in mm, clamped to the model's base height.
        """
        spacing = self.parameters["spacing_mm"]
        height = self.parameters["height"]
        rows = len(self.matrix_dem)
        cols = len(self.matrix_dem[0]) if rows else 0

        if rows == 0 or cols == 0:
            return 0.0

        # The matrix layout: row 0 is at y_model = height, row N is at y_model ≈ 0
        # x_model = j * spacing_mm, y_model = (rows - 1 - i) * spacing_mm
        col_f = x_mm / spacing
        row_f = (height - y_mm) / spacing

        col0 = max(0, min(cols - 1, int(math.floor(col_f))))
        col1 = max(0, min(cols - 1, col0 + 1))
        row0 = max(0, min(rows - 1, int(math.floor(row_f))))
        row1 = max(0, min(rows - 1, row0 + 1))

        z00 = self.matrix_dem[row0][col0].z
        z01 = self.matrix_dem[row0][col1].z
        z10 = self.matrix_dem[row1][col0].z
        z11 = self.matrix_dem[row1][col1].z

        tc = col_f - col0
        tr = row_f - row0
        tc = max(0.0, min(1.0, tc))
        tr = max(0.0, min(1.0, tr))

        z = (z00 * (1 - tc) * (1 - tr)
             + z01 * tc * (1 - tr)
             + z10 * (1 - tc) * tr
             + z11 * tc * tr)
        return round(z, 4)

    @staticmethod
    def _object_xml(obj_id, name, vertices, triangles):
        """Return the XML string for a single <object> element."""
        lines = []
        lines.append(
            '    <object id="{}" name="{}" type="model">'.format(obj_id, name)
        )
        lines.append("      <mesh>")
        lines.append("        <vertices>")
        for v in vertices:
            lines.append(
                '          <vertex x="{:.4f}" y="{:.4f}" z="{:.4f}"/>'.format(
                    v.x, v.y, v.z
                )
            )
        lines.append("        </vertices>")
        lines.append("        <triangles>")
        for tri in triangles:
            lines.append(
                '          <triangle v1="{}" v2="{}" v3="{}"/>'.format(
                    tri[0], tri[1], tri[2]
                )
            )
        lines.append("        </triangles>")
        lines.append("      </mesh>")
        lines.append("    </object>")
        return "\n".join(lines)

    def _build_model_xml(self, objects_xml):
        """Assemble the complete 3dmodel.model XML document."""
        lines = []
        lines.append('<?xml version="1.0" encoding="UTF-8"?>')
        lines.append(
            '<model unit="millimeter" xml:lang="en-US"'
            ' xmlns="{}">'.format(self._NS_3MF)
        )
        lines.append("  <resources>")
        for obj_xml in objects_xml:
            lines.append(obj_xml)
        lines.append("  </resources>")
        lines.append("  <build>")
        for idx in range(1, len(objects_xml) + 1):
            lines.append('    <item objectid="{}"/>'.format(idx))
        lines.append("  </build>")
        lines.append("</model>")
        return "\n".join(lines)

    @staticmethod
    def _content_types_xml():
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels"'
            ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="model"'
            ' ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            "</Types>"
        )

    @staticmethod
    def _rels_xml():
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '  <Relationship Target="/3D/3dmodel.model" Id="rel0"'
            ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            "</Relationships>"
        )

    def _write_3mf(self, objects_xml):
        """Write the 3MF ZIP archive to ``self.output_file``."""
        model_xml = self._build_model_xml(objects_xml)

        try:
            with zipfile.ZipFile(self.output_file, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(self._CONTENT_TYPES, self._content_types_xml())
                zf.writestr(self._RELS_FILE, self._rels_xml())
                zf.writestr(self._MODEL_FILE, model_xml)
        except (IOError, OSError):
            pass

    # ------------------------------------------------------------------
    # DEM cut helper (mirrors STL_Builder.cut_dem)
    # ------------------------------------------------------------------

    @staticmethod
    def _cut_dem(matrix_dem_build, resolution, x_min, y_min, x_max, y_max):
        rows = len(matrix_dem_build)
        cols = len(matrix_dem_build[0])
        dem = []
        for i in range(rows):
            aux = []
            for j in range(cols):
                x = matrix_dem_build[i][j].x
                y = matrix_dem_build[i][j].y
                if x_min <= x <= x_max and y_min <= y <= y_max:
                    aux.append(matrix_dem_build[i][j])
                elif 0 < (x - x_max) < resolution and y_min <= y <= y_max:
                    aux.append(matrix_dem_build[i][j])
                elif -resolution < (y - y_min) < 0 and x_min <= x <= x_max:
                    aux.append(matrix_dem_build[i][j])
                elif 0 < (x - x_max) < resolution and -resolution < (y - y_min) < 0:
                    aux.append(matrix_dem_build[i][j])
            if aux:
                dem.append(aux)
        return dem
