from langchain.tools import tool


@tool
def search_property_mock_tool(location: str) -> str:
    """Retrieve real estate details for properties in a given location."""
    return (
        "I found one apartment in that area. It features 3 rooms, "
        "2 bathrooms, and a beautifully designed living room."
    )
