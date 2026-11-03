import httpx
from typing import Optional

class StackOverflowSkill:
    def __init__(self):
        self.base_url = "https://api.stackexchange.com/2.3"
    
    async def search_questions(self, query: str, limit: int = 10, tagged: Optional[str] = None) -> dict:
        """Search Stack Overflow questions."""
        try:
            params = {
                "order": "desc",
                "sort": "relevance",
                "intitle": query,
                "site": "stackoverflow",
                "pagesize": min(limit, 100),
                "filter": "withbody"
            }
            
            if tagged:
                params["tagged"] = tagged
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/search/advanced",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": f"Search failed: {response.text}"}
                
                data = response.json()
                questions = []
                
                for item in data.get("items", []):
                    questions.append({
                        "question_id": item.get("question_id"),
                        "title": item.get("title"),
                        "body": item.get("body", "")[:300],
                        "tags": item.get("tags", []),
                        "answer_count": item.get("answer_count"),
                        "score": item.get("score"),
                        "view_count": item.get("view_count"),
                        "link": item.get("link"),
                        "owner": item.get("owner", {}).get("display_name"),
                        "created_date": item.get("creation_date"),
                        "last_activity_date": item.get("last_activity_date")
                    })
                
                return {
                    "status": "success",
                    "query": query,
                    "total": data.get("total", 0),
                    "results": len(questions),
                    "questions": questions,
                    "message": f"Found {len(questions)} question(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_question(self, question_id: int) -> dict:
        """Get a specific question with answers."""
        try:
            params = {
                "site": "stackoverflow",
                "filter": "withbody"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/questions/{question_id}",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Question not found"}
                
                data = response.json()
                if not data.get("items"):
                    return {"status": "error", "reason": "Question not found"}
                
                question = data["items"][0]
                
                # Get answers
                response = await client.get(
                    f"{self.base_url}/questions/{question_id}/answers",
                    params=params,
                    timeout=15
                )
                
                answers = []
                if response.status_code == 200:
                    answer_data = response.json()
                    for ans in answer_data.get("items", []):
                        answers.append({
                            "answer_id": ans.get("answer_id"),
                            "body": ans.get("body", "")[:500],
                            "score": ans.get("score"),
                            "is_accepted": ans.get("is_accepted"),
                            "owner": ans.get("owner", {}).get("display_name"),
                            "created_date": ans.get("creation_date")
                        })
                
                return {
                    "status": "success",
                    "question": {
                        "id": question.get("question_id"),
                        "title": question.get("title"),
                        "body": question.get("body"),
                        "tags": question.get("tags", []),
                        "score": question.get("score"),
                        "view_count": question.get("view_count"),
                        "answer_count": question.get("answer_count"),
                        "owner": question.get("owner", {}).get("display_name"),
                        "link": question.get("link")
                    },
                    "answers": answers[:5],
                    "message": f"Retrieved question with {len(answers)} answer(s)"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def search_by_tag(self, tag: str, limit: int = 10) -> dict:
        """Search questions by tag."""
        try:
            params = {
                "order": "desc",
                "sort": "newest",
                "tagged": tag,
                "site": "stackoverflow",
                "pagesize": min(limit, 100),
                "filter": "withbody"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/questions",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Search failed"}
                
                data = response.json()
                questions = []
                
                for item in data.get("items", []):
                    questions.append({
                        "question_id": item.get("question_id"),
                        "title": item.get("title"),
                        "tags": item.get("tags", []),
                        "score": item.get("score"),
                        "answer_count": item.get("answer_count"),
                        "link": item.get("link"),
                        "owner": item.get("owner", {}).get("display_name")
                    })
                
                return {
                    "status": "success",
                    "tag": tag,
                    "results": len(questions),
                    "questions": questions,
                    "message": f"Found {len(questions)} question(s) tagged '{tag}'"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_top_answerers(self, tag: str, limit: int = 10) -> dict:
        """Get top answerers for a tag."""
        try:
            params = {
                "order": "desc",
                "sort": "reputation",
                "tagged": tag,
                "site": "stackoverflow",
                "pagesize": min(limit, 100)
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/tags/{tag}/top-answerers/all_time",
                    params=params,
                    timeout=15
                )
                
                if response.status_code != 200:
                    return {"status": "error", "reason": "Tag not found"}
                
                data = response.json()
                answerers = []
                
                for user in data.get("items", []):
                    answerers.append({
                        "user_id": user.get("user_id"),
                        "name": user.get("user", {}).get("display_name"),
                        "reputation": user.get("user", {}).get("reputation"),
                        "answer_count": user.get("answer_count"),
                        "link": user.get("user", {}).get("link")
                    })
                
                return {
                    "status": "success",
                    "tag": tag,
                    "answerers": answerers,
                    "message": f"Found {len(answerers)} top answerers for '{tag}'"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute stackoverflow skill with structured args.

    REWRITTEN: get_question() requires an integer question_id — the old
    blind dispatch only ever passed the raw message string, which would
    never be a clean integer, so this action was effectively unreachable.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")
    limit = int(args.get("limit") or 10)

    if not action:
        return {
            "status": "info",
            "message": "Stack Overflow actions: search (query), get_question (question_id), by_tag (tag), top_answerers (tag)"
        }

    try:
        skill = StackOverflowSkill()

        if action == "search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"status": "error", "reason": "search requires a 'query'"}
            return await skill.search_questions(query, limit=limit, tagged=args.get("tag"))

        elif action == "get_question":
            question_id = args.get("question_id")
            if question_id is None:
                return {"status": "error", "reason": "get_question requires a numeric 'question_id'"}
            try:
                question_id = int(question_id)
            except (TypeError, ValueError):
                return {"status": "error", "reason": "question_id must be a number"}
            return await skill.get_question(question_id)

        elif action == "by_tag":
            tag = (args.get("tag") or "").strip()
            if not tag:
                return {"status": "error", "reason": "by_tag requires a 'tag'"}
            return await skill.search_by_tag(tag, limit=limit)

        elif action == "top_answerers":
            tag = (args.get("tag") or "").strip()
            if not tag:
                return {"status": "error", "reason": "top_answerers requires a 'tag'"}
            return await skill.get_top_answerers(tag, limit=limit)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: search, get_question, by_tag, top_answerers"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
