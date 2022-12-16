@external
@nonreentrant("foo")
def foo() -> uint256:
    if block.number % 2 == 0:
        return 5

    self._bar()
    x: uint256 = self._baz()

    return 0

@external
def bar():
    y: uint256 = 2

@external
@nonreentrant("baz")
def baz():
    z: uint256 = 3

@external
def qux() -> uint256:
    return 7

@internal
def _bar():
    x: uint256 = 1

@internal
def _baz() -> uint256:
    if block.number % 2 == 0:
        return 5

    x: uint256 = 1

    return 0