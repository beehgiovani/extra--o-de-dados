"""Compatibilidade com o nome antigo do extrator.

O ponto de entrada principal do projeto passou a ser
``extrator_imobiliario_mestre.py``. Este arquivo permanece para não quebrar
automatizações e comandos já existentes.
"""

from extrator_imobiliario_mestre import *  # noqa: F401,F403


if __name__ == "__main__":
    from extrator_imobiliario_mestre import main

    raise SystemExit(main())
