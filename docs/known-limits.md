# Known limits

- An icon the app does not label is named by the captioner, in a few English words about how
  it looks ("a gear", "a magnifying glass"), whatever the app's language. It says what an
  icon looks like, not what it does, and a small or unusual one can come out wrong.
- Every step sends TypeSafe a picture of the frontmost window. Anything visible there leaves
  the machine, including text typed into a field; nothing is blanked out. The browser backend
  sends no picture.
- The picture travels as a TypeSafe image asset, behind the `TypeSafe-Danger-Zone-Images`
  header: an interface TypeSafe calls Dreamer-ready, whose format and accuracy may still
  change. When System One refuses the picture, the step goes on without it.
- The desktop perception needs PyTorch, which ships no build for an Intel Mac; there only the
  browser backend runs.
- Two identical labels in one row, or in no row at all, get only a coarse region hint and
  split the vote. Ones in different rows are told apart by the text beside them.
- Only the main display is captured.
- A repeated action whose effect never shows on screen (a third "New note" in an app that
  lists nothing) reads as a cycle and stops the run: the capture is the only witness.
- Using the machine during an `--act` run fights it for focus and the cursor.
- The site catalog is small on purpose; the writer covers the rest.
- Stacked short lines merge into one item, so a list of checkboxes ("Arrives in 2-4
  days", "Free Shipping", "Local Pickup") that the app does not publish through
  accessibility is one click target, aimed at its middle.
