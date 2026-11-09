"""Console entry point for the Windows onedir distribution."""

if __name__ == "__main__":
    # PyInstaller diverts frozen multiprocessing children here before engine imports.
    from multiprocessing import freeze_support

    freeze_support()

    from chess_engine.stockconda import UCIEngine

    UCIEngine().run()
