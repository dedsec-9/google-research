# coding=utf-8
# Copyright 2021 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Conversion from smiles to 3D TFDatarecord."""

from absl import app
from absl import flags

import pandas as pd
from rdkit import Chem
from rdkit.Chem import AllChem
import tensorflow as tf

from smu import dataset_pb2
from smu.geometry import utilities
from smu.parser import smu_utils_lib

FLAGS = flags.FLAGS

flags.DEFINE_string("smiles", None, "Smiles input file")
flags.DEFINE_string("output", None, "TFRecord output file")
flags.DEFINE_integer("nprocess", 0, "Number of items to process")


def contains_aromatic(mol):
  """Returns True of `mol` contains any aromatic atoms."""
  for atom in mol.GetAtoms():
    if atom.GetIsAromatic():
      return True
  return False


def smi23d(unused_argv):
  """Converts a smiles file to 3D TFDatarecord proto Conformer."""
  del unused_argv

  df = pd.read_csv(FLAGS.smiles)
  nprocess = FLAGS.nprocess
  if nprocess == 0:
    nprocess = df.shape[0]

  with tf.io.TFRecordWriter(FLAGS.output) as file_writer:
    for ndx in range(0, nprocess):
      smiles = df.iloc[ndx, 0]
      name = df.iloc[ndx, 1]
      mol = Chem.MolFromSmiles(smiles)
      if contains_aromatic(mol):
        continue
      mol = Chem.AddHs(mol)
      AllChem.EmbedMolecule(mol, AllChem.ETKDG())
      if mol.GetNumConformers() == 0:
        continue

      natoms = mol.GetNumAtoms()

      conf = mol.GetConformer(0)
      geom = dataset_pb2.Geometry()
      for i in range(0, natoms):
        atom = dataset_pb2.Geometry.AtomPos()
        atom.x = conf.GetAtomPosition(i).x / smu_utils_lib.BOHR_TO_ANGSTROMS
        atom.y = conf.GetAtomPosition(i).y / smu_utils_lib.BOHR_TO_ANGSTROMS
        atom.z = conf.GetAtomPosition(i).z / smu_utils_lib.BOHR_TO_ANGSTROMS
        geom.atom_positions.append(atom)

      conformer = dataset_pb2.Conformer()
      conformer.bond_topologies.append(utilities.molecule_to_bond_topology(mol))
      conformer.bond_topologies[-1].smiles = smiles
      conformer.optimized_geometry.CopyFrom(geom)
      conformer.conformer_id = int(name)
      file_writer.write(conformer.SerializeToString())


if __name__ == "__main__":
  flags.mark_flag_as_required("smiles")
  app.run(smi23d)
