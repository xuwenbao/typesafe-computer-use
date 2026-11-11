"""The desktop perception's models: text, icons, and what an icon is for.

A capture becomes OCR lines (`ocr`), icon boxes (`detect`), the two fused into elements (`fuse`),
a caption for each icon that has no text (`caption`), and the numbered image the classifier sees
(`marks`). `weights` finds the model files. Importing any of these loads no model and imports no
PyTorch: an Intel Mac, which has no PyTorch, still imports the package and runs its tests.
"""
