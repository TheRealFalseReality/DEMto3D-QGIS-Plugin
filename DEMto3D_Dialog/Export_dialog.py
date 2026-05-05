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

from __future__ import absolute_import
import math
import os

from qgis.PyQt.QtWidgets import QMessageBox
from qgis.PyQt.QtWidgets import QDialog

from ..model_builder.Model_Builder import Model
from ..model_builder.STL_Builder import STL
from ..model_builder.ThreeMF_Builder import ThreeMF


class Export(QDialog):

    Model = None
    STL = None
    ThreeMF = None

    def __init__(self, mainDialog, parameters, file_name):
        QDialog.__init__(self)
        self.mainDlg = mainDialog
        self.parameters = parameters
        self.stl_file = file_name
        # Determine format from file extension
        self._is_3mf = file_name.lower().endswith(".3mf")
        self.prepareUi(True)
        self.do_model()

    def do_model(self):
        maxVal = int(math.ceil(self.parameters["height"] / self.parameters["spacing_mm"]) + 1)
        self.mainDlg.ui.progressBar.setMaximum(maxVal)
        self.mainDlg.ui.progressBar.setValue(0)
        self.mainDlg.ui.cancelProgressToolButton.clicked.connect(self.cancel_model)
        self.mainDlg.ui.ProgressLabel.setText(self.tr("Building geometry"))

        self.Model = Model(self.parameters)
        self.Model.updateProgress.connect(lambda: self.mainDlg.ui.progressBar.setValue(self.mainDlg.ui.progressBar.value() + 1))
        if self._is_3mf:
            self.Model.finished.connect(self.do_3mf_model)
        else:
            self.Model.finished.connect(self.do_stl_model)
        self.Model.start()

    def cancel_model(self):
        self.Model.quit = True

    # ------------------------------------------------------------------
    # STL export path
    # ------------------------------------------------------------------

    def do_stl_model(self):
        self.mainDlg.ui.progressBar.setValue(0)
        if self.Model.quit:
            self.prepareUi(False)
            QMessageBox.information(self.mainDlg, self.mainDlg.tr("Attention"), self.mainDlg.tr("Process cancelled"))
        else:
            self.mainDlg.ui.cancelProgressToolButton.clicked.connect(self.cancel_stl_model)
            self.mainDlg.ui.ProgressLabel.setText(self.tr("Creating STL file"))
            dem_matrix = self.Model.get_model()
            rows = dem_matrix.__len__()
            cols = dem_matrix[0].__len__()
            maxVal = rows * cols * 2
            self.mainDlg.ui.progressBar.setMaximum(maxVal)

            self.STL = STL(self.parameters, self.stl_file, dem_matrix)
            self.STL.updateProgress.connect(lambda: self.mainDlg.ui.progressBar.setValue(self.mainDlg.ui.progressBar.value() + 1))
            self.STL.finished.connect(self.finish_stl_model)
            self.STL.start()

    def cancel_stl_model(self):
        self.STL.quit = True

    def finish_stl_model(self):
        self.prepareUi(False)
        self.mainDlg.ui.progressBar.setValue(0)
        if self.STL.quit:
            if os.path.exists(self.stl_file):
                os.remove(self.stl_file)
            QMessageBox.information(self.mainDlg, self.mainDlg.tr("Attention"), self.mainDlg.tr("Process cancelled"))
        else:
            QMessageBox.information(self.mainDlg, self.mainDlg.tr("Attention"), self.mainDlg.tr("STL model generated"))

    # ------------------------------------------------------------------
    # 3MF export path
    # ------------------------------------------------------------------

    def do_3mf_model(self):
        self.mainDlg.ui.progressBar.setValue(0)
        if self.Model.quit:
            self.prepareUi(False)
            QMessageBox.information(self.mainDlg, self.mainDlg.tr("Attention"), self.mainDlg.tr("Process cancelled"))
        else:
            self.mainDlg.ui.cancelProgressToolButton.clicked.connect(self.cancel_3mf_model)
            self.mainDlg.ui.ProgressLabel.setText(self.tr("Creating 3MF file"))
            dem_matrix = self.Model.get_model()
            rows = dem_matrix.__len__()
            cols = dem_matrix[0].__len__()
            maxVal = rows * cols * 2
            self.mainDlg.ui.progressBar.setMaximum(maxVal)

            trail_layer = self.parameters.get("trail_layer", None)
            self.ThreeMF = ThreeMF(self.parameters, self.stl_file, dem_matrix, trail_layer)
            self.ThreeMF.updateProgress.connect(lambda: self.mainDlg.ui.progressBar.setValue(self.mainDlg.ui.progressBar.value() + 1))
            self.ThreeMF.finished.connect(self.finish_3mf_model)
            self.ThreeMF.start()

    def cancel_3mf_model(self):
        self.ThreeMF.quit = True

    def finish_3mf_model(self):
        self.prepareUi(False)
        self.mainDlg.ui.progressBar.setValue(0)
        if self.ThreeMF.quit:
            if os.path.exists(self.stl_file):
                os.remove(self.stl_file)
            QMessageBox.information(self.mainDlg, self.mainDlg.tr("Attention"), self.mainDlg.tr("Process cancelled"))
        else:
            QMessageBox.information(self.mainDlg, self.mainDlg.tr("Attention"), self.mainDlg.tr("3MF model generated"))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def finish_model(self):
        """Legacy alias kept for any external callers."""
        if self._is_3mf:
            self.finish_3mf_model()
        else:
            self.finish_stl_model()

    def prepareUi(self, start):
        if start:
            self.mainDlg.ui.ProgressLabel.show()
        else:
            self.mainDlg.ui.ProgressLabel.hide()
        self.mainDlg.ui.cancelProgressToolButton.setEnabled(start)
        self.mainDlg.ui.groupBox.setEnabled(not start)
        self.mainDlg.ui.groupBox_1.setEnabled(not start)
        self.mainDlg.ui.groupBox_2.setEnabled(not start)
        self.mainDlg.ui.groupBox_3.setEnabled(not start)
        self.mainDlg.ui.groupBox_5.setEnabled(not start)
        self.mainDlg.ui.groupBox_trail.setEnabled(not start)
        self.mainDlg.ui.ParamPushButton.setEnabled(not start)
        self.mainDlg.ui.STLToolButton.setEnabled(not start)
        self.mainDlg.ui.CancelToolButton.setEnabled(not start)
