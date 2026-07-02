"""LLM 生成 pandas コードの実行サンドボックス。

2 段構え:
1. **RestrictedPython**（導入時）: AST レベルで import・dunder 属性アクセス・危険構文を
   コンパイル段階で禁止する“本格”サンドボックス。`df.__class__.__bases__...` のような
   エスケープ経路も塞ぐ（deny-list 文字列マッチでは防ぎきれない部分）。
2. **フォールバック**（未導入時）: builtins 最小化 + deny-list によるハードニング済み exec。

いずれも I/O 系メソッド名（read_*/to_*/pickle 等）の deny-list を多重防御として併用する。
完全な隔離（別プロセス・リソース制限）ではない点は明記する。ローカル利用前提。
"""

from __future__ import annotations


def restrictedpython_available() -> bool:
    try:
        import RestrictedPython  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _reject(code: str, forbidden: tuple) -> None:
    low = code.lower()
    hit = [tok for tok in forbidden if tok in low]
    if hit:
        raise ValueError(f"安全のため実行を拒否しました（禁止トークン: {hit}）。コード:\n{code}")


def safe_exec(code: str, namespace: dict, forbidden: tuple, *, result_var: str = "result"):
    """code を制限環境で実行し namespace[result_var] を返す。

    namespace には実行に必要な変数（pd, DataFrame 群など）を入れておく。
    """
    _reject(code, forbidden)  # 多重防御: まず文字列 deny-list

    if restrictedpython_available():
        return _run_restricted(code, namespace, result_var)
    return _run_plain(code, namespace, result_var)


def _run_restricted(code: str, namespace: dict, result_var: str):
    from RestrictedPython import compile_restricted
    from RestrictedPython.Guards import (
        guarded_iter_unpack_sequence,
        safe_builtins,
        safer_getattr,
    )

    try:
        byte_code = compile_restricted(code, filename="<tableqa>", mode="exec")
    except SyntaxError as e:
        raise ValueError(f"サンドボックスがコードを拒否しました（制限構文）: {e}\nコード:\n{code}") from e

    # pandas の公開メソッド（アンダースコア始まりでない）だけ許可する getattr。
    # __class__ / __globals__ 等のエスケープ経路は safer_getattr が遮断する。
    builtins = dict(safe_builtins)
    builtins.update({
        "sum": sum, "min": min, "max": max, "sorted": sorted, "len": len,
        "range": range, "abs": abs, "round": round, "enumerate": enumerate,
        "zip": zip, "list": list, "dict": dict, "set": set, "tuple": tuple,
        "float": float, "int": int, "str": str, "bool": bool, "print": print,
    })
    glb = {
        "__builtins__": builtins,
        "_getattr_": safer_getattr,
        "_getitem_": lambda obj, key: obj[key],
        "_getiter_": iter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_write_": lambda obj: obj,  # 渡すのはコピーなので in-place 変更を許可
        **namespace,
    }
    try:
        exec(byte_code, glb)  # noqa: S102 RestrictedPython でコンパイル済み
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"生成コードの実行に失敗しました: {e}\nコード:\n{code}") from e
    if result_var not in glb:
        raise RuntimeError(f"コードが `{result_var}` を定義しませんでした。コード:\n{code}")
    return glb[result_var]


def _run_plain(code: str, namespace: dict, result_var: str):
    """RestrictedPython 非導入時のフォールバック（builtins 最小化 exec）。"""
    _SAFE_BUILTINS = {
        "len": len, "range": range, "sum": sum, "min": min, "max": max, "abs": abs,
        "round": round, "sorted": sorted, "list": list, "dict": dict, "set": set,
        "tuple": tuple, "float": float, "int": int, "str": str, "bool": bool,
        "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
        "any": any, "all": all, "print": print,
    }
    ns = {"__builtins__": _SAFE_BUILTINS, **namespace}
    print("[TableQA] RestrictedPython 未導入のため簡易サンドボックスで実行します"
          "（推奨: pip install RestrictedPython）")
    try:
        exec(code, ns)  # noqa: S102
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"生成コードの実行に失敗しました: {e}\nコード:\n{code}") from e
    if result_var not in ns:
        raise RuntimeError(f"コードが `{result_var}` を定義しませんでした。コード:\n{code}")
    return ns[result_var]
