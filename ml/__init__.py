"""
ml/ — Paquete de Machine Learning: Meta-Labeling con LightGBM
=============================================================
Implementa el estándar de 2 capas para trading algorítmico:

  Capa 1: Señal Técnica (signals/indicators.py) → Long/No-Trade
  Capa 2: Meta-Model (ml/) → P(TP antes de SL) → Filtro + Bet Sizing

Módulos:
  - features.py  : Feature Engineering estacionario
  - labeling.py  : Triple Barrier Labeling (López de Prado, 2018)
  - model.py     : Wrapper LightGBM para inferencia en producción
  - train.py     : Pipeline offline de entrenamiento (script standalone)
"""
