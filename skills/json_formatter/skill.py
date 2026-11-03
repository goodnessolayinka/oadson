import json
from typing import Optional, Any

class JSONFormatterSkill:
    def __init__(self):
        pass
    
    def format(self, json_str: str, indent: int = 2, sort_keys: bool = False) -> dict:
        """Format and beautify JSON."""
        try:
            # Parse JSON
            data = json.loads(json_str)
            
            # Format with indentation
            formatted = json.dumps(
                data,
                indent=indent,
                sort_keys=sort_keys,
                ensure_ascii=False
            )
            
            return {
                "status": "success",
                "formatted": formatted,
                "lines": len(formatted.split("\n")),
                "message": "JSON formatted successfully"
            }
        
        except json.JSONDecodeError as e:
            return {
                "status": "error",
                "reason": f"Invalid JSON: {str(e)}",
                "error_line": e.lineno,
                "error_column": e.colno
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def validate(self, json_str: str) -> dict:
        """Validate JSON syntax."""
        try:
            data = json.loads(json_str)
            
            return {
                "status": "success",
                "valid": True,
                "message": "Valid JSON",
                "root_type": type(data).__name__,
                "size": len(json_str)
            }
        
        except json.JSONDecodeError as e:
            return {
                "status": "error",
                "valid": False,
                "reason": str(e),
                "error_line": e.lineno,
                "error_column": e.colno,
                "message": f"Invalid JSON at line {e.lineno}, column {e.colno}"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def minify(self, json_str: str) -> dict:
        """Minify JSON (remove whitespace)."""
        try:
            data = json.loads(json_str)
            minified = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            
            original_size = len(json_str)
            minified_size = len(minified)
            savings = ((original_size - minified_size) / original_size) * 100
            
            return {
                "status": "success",
                "minified": minified,
                "original_size": original_size,
                "minified_size": minified_size,
                "savings_percent": round(savings, 2),
                "message": f"Minified. Saved {round(savings, 2)}%"
            }
        
        except json.JSONDecodeError as e:
            return {"status": "error", "reason": f"Invalid JSON: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def prettify(self, json_str: str, indent: int = 4) -> dict:
        """Prettify JSON with custom indentation."""
        try:
            data = json.loads(json_str)
            pretty = json.dumps(data, indent=indent, sort_keys=True, ensure_ascii=False)
            
            return {
                "status": "success",
                "prettified": pretty,
                "message": "JSON prettified"
            }
        
        except json.JSONDecodeError as e:
            return {"status": "error", "reason": f"Invalid JSON: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_schema(self, json_str: str) -> dict:
        """Generate JSON schema from data."""
        try:
            data = json.loads(json_str)
            
            schema = self._infer_schema(data)
            
            return {
                "status": "success",
                "schema": schema,
                "message": "Schema inferred"
            }
        
        except json.JSONDecodeError as e:
            return {"status": "error", "reason": f"Invalid JSON: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def _infer_schema(self, obj: Any) -> dict:
        """Infer JSON schema from object."""
        if isinstance(obj, dict):
            properties = {}
            for key, value in obj.items():
                properties[key] = self._infer_schema(value)
            return {"type": "object", "properties": properties}
        
        elif isinstance(obj, list):
            if obj:
                items = self._infer_schema(obj[0])
            else:
                items = {"type": "unknown"}
            return {"type": "array", "items": items}
        
        elif isinstance(obj, bool):
            return {"type": "boolean"}
        
        elif isinstance(obj, int):
            return {"type": "integer"}
        
        elif isinstance(obj, float):
            return {"type": "number"}
        
        elif isinstance(obj, str):
            return {"type": "string"}
        
        elif obj is None:
            return {"type": "null"}
        
        else:
            return {"type": type(obj).__name__}
    
    def compare(self, json1_str: str, json2_str: str) -> dict:
        """Compare two JSON objects."""
        try:
            data1 = json.loads(json1_str)
            data2 = json.loads(json2_str)
            
            are_equal = data1 == data2
            
            differences = []
            if not are_equal:
                differences = self._find_differences(data1, data2)
            
            return {
                "status": "success",
                "equal": are_equal,
                "differences": differences,
                "message": "Equal" if are_equal else f"Found {len(differences)} difference(s)"
            }
        
        except json.JSONDecodeError as e:
            return {"status": "error", "reason": f"Invalid JSON: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def _find_differences(self, obj1: Any, obj2: Any, path: str = "root") -> list:
        """Find differences between two objects."""
        differences = []
        
        if type(obj1) != type(obj2):
            differences.append(f"{path}: type mismatch ({type(obj1).__name__} vs {type(obj2).__name__})")
        
        elif isinstance(obj1, dict):
            all_keys = set(obj1.keys()) | set(obj2.keys())
            for key in all_keys:
                if key not in obj1:
                    differences.append(f"{path}.{key}: missing in first object")
                elif key not in obj2:
                    differences.append(f"{path}.{key}: missing in second object")
                else:
                    differences.extend(self._find_differences(obj1[key], obj2[key], f"{path}.{key}"))
        
        elif isinstance(obj1, list):
            if len(obj1) != len(obj2):
                differences.append(f"{path}: length mismatch ({len(obj1)} vs {len(obj2)})")
            else:
                for i, (item1, item2) in enumerate(zip(obj1, obj2)):
                    differences.extend(self._find_differences(item1, item2, f"{path}[{i}]"))
        
        elif obj1 != obj2:
            differences.append(f"{path}: value mismatch ({obj1} vs {obj2})")
        
        return differences

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute json_formatter skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "JSON actions: format, validate, minify, prettify, schema (need json_str), compare (needs json1/json2)"
        }

    try:
        skill = JSONFormatterSkill()

        if action == "compare":
            json1 = args.get("json1") or ""
            json2 = args.get("json2") or ""
            if not json1 or not json2:
                return {"status": "error", "reason": "compare requires 'json1' and 'json2'"}
            return skill.compare(json1, json2)

        json_str = args.get("json_str") or ""
        if not json_str.strip():
            return {"status": "error", "reason": f"{action} requires 'json_str'"}

        if action == "format":
            return skill.format(json_str, indent=int(args.get("indent") or 2), sort_keys=bool(args.get("sort_keys", False)))
        elif action == "validate":
            return skill.validate(json_str)
        elif action == "minify":
            return skill.minify(json_str)
        elif action == "prettify":
            return skill.prettify(json_str, indent=int(args.get("indent") or 4))
        elif action == "schema":
            return skill.get_schema(json_str)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: format, validate, minify, prettify, schema, compare"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
