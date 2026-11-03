import math
import os
from typing import Optional

class CalculatorSkill:
    def __init__(self):
        pass
    
    def evaluate(self, expression: str) -> dict:
        """Evaluate ANY math expression safely."""
        try:
            # Full math module + safe operations
            safe_dict = {
                # Trig
                "sin": math.sin, "cos": math.cos, "tan": math.tan,
                "asin": math.asin, "acos": math.acos, "atan": math.atan,
                "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
                "degrees": math.degrees, "radians": math.radians,
                
                # Exponential & Logarithm
                "exp": math.exp, "log": math.log, "log10": math.log10,
                "log2": math.log2, "sqrt": math.sqrt, "pow": pow,
                
                # Rounding
                "ceil": math.ceil, "floor": math.floor, "fabs": math.fabs,
                
                # Constants
                "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
                
                # Other utilities
                "abs": abs, "round": round, "min": min, "max": max,
                "sum": sum, "len": len,
                
                # Factorial, combinations
                "factorial": math.factorial, "comb": math.comb, "perm": math.perm,
                
                # Complex numbers
                "complex": complex,
                
                # GCD, LCM
                "gcd": math.gcd, "lcm": math.lcm,
                
                # Hyperbolic & Special
                "acosh": math.acosh, "asinh": math.asinh, "atanh": math.atanh,
                "erf": math.erf, "erfc": math.erfc, "gamma": math.gamma,
                
                "__builtins__": {}
            }
            
            result = eval(expression, {"__builtins__": {}}, safe_dict)
            
            return {
                "status": "success",
                "expression": expression,
                "result": result,
                "message": f"{expression} = {result}"
            }
        
        except SyntaxError:
            return {"status": "error", "reason": f"Invalid syntax: {expression}"}
        except ZeroDivisionError:
            return {"status": "error", "reason": "Division by zero"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def percentage(self, part: float, whole: float) -> dict:
        """Calculate percentage."""
        try:
            if whole == 0:
                return {"status": "error", "reason": "Whole cannot be zero"}
            
            result = (part / whole) * 100
            
            return {
                "status": "success",
                "part": part,
                "whole": whole,
                "percentage": round(result, 2),
                "message": f"{part} is {round(result, 2)}% of {whole}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def percentage_change(self, original: float, new: float) -> dict:
        """Calculate percentage change."""
        try:
            if original == 0:
                return {"status": "error", "reason": "Original value cannot be zero"}
            
            change = ((new - original) / original) * 100
            
            return {
                "status": "success",
                "original": original,
                "new": new,
                "percentage_change": round(change, 2),
                "direction": "increase" if change > 0 else "decrease",
                "message": f"{round(abs(change), 2)}% {'increase' if change > 0 else 'decrease'} from {original} to {new}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def compound_interest(self, principal: float, rate: float, time: float, n: int = 12) -> dict:
        """Calculate compound interest. A = P(1 + r/n)^(nt)"""
        try:
            amount = principal * (1 + rate / (100 * n)) ** (n * time)
            interest = amount - principal
            
            return {
                "status": "success",
                "principal": principal,
                "rate": rate,
                "time_years": time,
                "compounds_per_year": n,
                "final_amount": round(amount, 2),
                "total_interest": round(interest, 2),
                "message": f"${principal} at {rate}% for {time} years = ${round(amount, 2)}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

async def run(payload):
    """
    Entry point for OADSON skill execution.

    REWRITTEN: previously this ran skill.evaluate(message) against the
    ENTIRE raw user sentence — "So I want you to run any random 2 skills
    can you do that in sequence" is not valid Python, so eval() correctly
    SyntaxErrors on it, and that error (echoing the whole sentence back)
    is exactly what showed up as "Invalid syntax: So I want you to run
    any random 2 skills..." Now the model extracts a clean expression
    (e.g. "25*4") into args.expression per manifest.json's schema —
    eval() itself was already reasonably sandboxed (empty __builtins__,
    explicit allowlist), it just needed real math text, not a sentence.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Calculator actions: evaluate (expression), percentage (part/whole), percentage_change (original/new), compound_interest (principal/rate/time)"
        }

    try:
        skill = CalculatorSkill()

        if action == "evaluate":
            expression = (args.get("expression") or "").strip()
            if not expression:
                return {"status": "error", "reason": "evaluate requires an 'expression'"}
            return skill.evaluate(expression)

        elif action == "percentage":
            if "part" not in args or "whole" not in args:
                return {"status": "error", "reason": "percentage requires 'part' and 'whole'"}
            return skill.percentage(float(args["part"]), float(args["whole"]))

        elif action == "percentage_change":
            if "original" not in args or "new" not in args:
                return {"status": "error", "reason": "percentage_change requires 'original' and 'new'"}
            return skill.percentage_change(float(args["original"]), float(args["new"]))

        elif action == "compound_interest":
            required = ["principal", "rate", "time"]
            if any(k not in args for k in required):
                return {"status": "error", "reason": "compound_interest requires 'principal', 'rate', and 'time'"}
            n = int(args.get("compounds_per_year") or 12)
            return skill.compound_interest(float(args["principal"]), float(args["rate"]), float(args["time"]), n=n)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: evaluate, percentage, percentage_change, compound_interest"
            }

    except (TypeError, ValueError) as e:
        return {"status": "error", "reason": f"Invalid numeric argument: {str(e)}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
