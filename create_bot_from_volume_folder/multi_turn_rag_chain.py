# Databricks notebook source
# MAGIC %pip install -U -qqqq --upgrade databricks-agents mlflow mlflow-skinny databricks-vectorsearch langchain langchain_core langchain_community 

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

from operator import itemgetter
import mlflow
import os

from databricks.vector_search.client import VectorSearchClient

from langchain_community.chat_models import ChatDatabricks
from langchain_community.vectorstores import DatabricksVectorSearch

from langchain_core.runnables import RunnableLambda
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import (
    PromptTemplate,
    ChatPromptTemplate,
    MessagesPlaceholder,
)
from langchain_core.runnables import RunnablePassthrough, RunnableBranch
from langchain_core.messages import HumanMessage, AIMessage

## Enable MLflow Tracing
mlflow.langchain.autolog()


############
# Helper functions
############
# Return the string contents of the most recent message from the user
def extract_user_query_string(chat_messages_array):
    return chat_messages_array[-1]["content"]


# Return the chat history, which is is everything before the last question
def extract_chat_history(chat_messages_array):
    return chat_messages_array[:-1]


# Load the chain's configuration
model_config = mlflow.models.ModelConfig(development_config="rag_chain_config.yaml")

databricks_resources = model_config.get("databricks_resources")
retriever_config = model_config.get("retriever_config")
llm_config = model_config.get("llm_config")

############
# Connect to the Vector Search Index
############
vs_client = VectorSearchClient(disable_notice=True)
vs_index = vs_client.get_index(
    endpoint_name=databricks_resources.get("vector_search_endpoint_name"),
    index_name=retriever_config.get("vector_search_index"),
)
vector_search_schema = retriever_config.get("schema")

############
# Turn the Vector Search index into a LangChain retriever
############
def get_retriever(info):
    print("filter_value:",info["filter_value"])

    kwargs = retriever_config.get("parameters")
    kwargs['filter'] = {'topic' :info["filter_value"].strip()}

    retriever =  DatabricksVectorSearch(
        vs_index,
        text_column=vector_search_schema.get("chunk_text"),
        columns=[
            vector_search_schema.get("primary_key"),
            vector_search_schema.get("chunk_text"),
            vector_search_schema.get("document_uri"),
            vector_search_schema.get("topic"),
            vector_search_schema.get("title")
        ],
    ).as_retriever(search_kwargs=kwargs)

    return retriever.invoke(info["rephrased_question"])

retriever =  DatabricksVectorSearch(
    vs_index,
    text_column=vector_search_schema.get("chunk_text"),
    columns=[
        vector_search_schema.get("primary_key"),
        vector_search_schema.get("chunk_text"),
        vector_search_schema.get("document_uri"),
        vector_search_schema.get("topic"),
        vector_search_schema.get("title")
    ],
).as_retriever(search_kwargs=retriever_config.get("parameters"))

############
# Required to:
# 1. Enable the RAG Studio Review App to properly display retrieved chunks
# 2. Enable evaluation suite to measure the retriever
############

mlflow.models.set_retriever_schema(
    primary_key=vector_search_schema.get("primary_key"),
    text_column=vector_search_schema.get("chunk_text"),
    doc_uri=vector_search_schema.get(
        "document_uri"
    ),  # Review App uses `doc_uri` to display chunks from the same document in a single view
)


############
# Method to format the docs returned by the retriever into the prompt
############
def format_context(docs):
    chunk_template = retriever_config.get("chunk_template")
    chunk_contents = [
        chunk_template.format(
            chunk_text=d.page_content,
            document_uri=d.metadata[vector_search_schema.get("title")],
        )
        for d in docs
    ]
    return "".join(chunk_contents)


############
# Prompt Template for generation
############
prompt = ChatPromptTemplate.from_messages(
    [
        (  # System prompt contains the instructions
            "system",
            llm_config.get("llm_system_prompt_template"),
        ),
        # If there is history, provide it.
        # Note: This chain does not compress the history, so very long converastions can overflow the context window.
        MessagesPlaceholder(variable_name="formatted_chat_history"),
        # User's most current question
        ("user", "{question}"),
    ]
)


# Format the converastion history to fit into the prompt template above.
def format_chat_history_for_prompt(chat_messages_array):
    history = extract_chat_history(chat_messages_array)
    formatted_chat_history = []
    if len(history) > 0:
        for chat_message in history:
            if chat_message["role"] == "user":
                formatted_chat_history.append(
                    HumanMessage(content=chat_message["content"])
                )
            elif chat_message["role"] == "assistant":
                formatted_chat_history.append(
                    AIMessage(content=chat_message["content"])
                )
    return formatted_chat_history


############
# Prompt Template for query rewriting to allow converastion history to work - this will translate a query such as "how does it work?" after a question such as "what is spark?" to "how does spark work?".
############
query_rewrite_template = """Based on the chat history below, we want you to generate a query for an external data source to retrieve relevant documents so that we can better answer the question. The query should be in natural language. The external data source uses similarity search to search for relevant documents in a vector space. So the query should be similar to the relevant documents semantically. Answer with only the query. Do not add explanation.

Chat history: {chat_history}

Question: {question}"""


query_rewrite_prompt = PromptTemplate(
    template=query_rewrite_template,
    input_variables=["chat_history", "question"],
)

get_topic_template = """Based on the question provieded by the users classify the topic in one of below categories delimited by a comma only provide the classification and no other text.
--- list of topics
cancer vaccines,stem cell therapy,cellular reprogramming
--- end list of topics
Question: {question}"""

get_topic_prompt = PromptTemplate(
    template=get_topic_template,
    input_variables=["question"],
)

############
# FM for generation
############
model = ChatDatabricks(
    endpoint=databricks_resources.get("llm_endpoint_name"),
    extra_params=llm_config.get("llm_parameters"),
)

############
# RAG Chain
############
chain = (
    {
        "question": itemgetter("messages") | RunnableLambda(extract_user_query_string),
        "chat_history": itemgetter("messages") | RunnableLambda(extract_chat_history),
        "formatted_chat_history": itemgetter("messages")
        | RunnableLambda(format_chat_history_for_prompt),
    }
    | RunnablePassthrough()
    | {
        "rephrased_question": RunnableBranch(  # Only re-write the question if there is a chat history
            (
                lambda x: len(x["chat_history"]) > 0,
                query_rewrite_prompt | model | StrOutputParser(),
            ),
            itemgetter("question")
        ),
        "retriever" : itemgetter("question")|retriever,
        "question": itemgetter("question"),
        "formatted_chat_history" : itemgetter("formatted_chat_history")
    }
    | RunnablePassthrough()
    |{"context" :{ "rephrased_question": itemgetter("rephrased_question"),
                  "filter_value": itemgetter("rephrased_question")| get_topic_prompt | model | StrOutputParser()}
      | RunnableLambda(get_retriever) | RunnableLambda(format_context),
        "formatted_chat_history": itemgetter("formatted_chat_history"),
        "question": itemgetter("question")
    }
    | prompt
    | model
    | StrOutputParser()
)

## Tell MLflow logging where to find your chain.
# `mlflow.models.set_model(model=...)` function specifies the LangChain chain to use for evaluation and deployment.  This is required to log this chain to MLflow with `mlflow.langchain.log_model(...)`.

mlflow.models.set_model(model=chain)

# COMMAND ----------

input_example = {
        "messages": [
            {
                "role": "user",
                "content": "Does age of a person have an impact on efficacy of HPV Vaccines?",
            },
            # {
            #     "role": "assistant",
            #     "content": "Assistant's reply",
            # },
            # {
            #     "role": "user",
            #     "content": "User's next question",
            # },
        ]
    }



input_example = {
        "messages": [
            {
                "role": "user",
                "content": "What can  multipotent mesenchymal stem cells be used for treatment for?",
            },
            {
                "role": "assistant",
                "content": "Multipotent mesenchymal stem cells (MSCs) have demonstrated promise in treating various human diseases, including:\n\n1. Pulmonary dysfunctions\n2. Neurological disorders\n3. Endocrine/metabolic diseases\n4. Skin burns\n5. Cardiovascular conditions\n6. Reproductive disorders\n7. Systemic lupus erythematosus\n8. Multiple sclerosis\n9. Acute respiratory distress syndrome (ARDS)\n10. Chronic obstructive pulmonary disease (COPD)\n11. COVID-19\n12. Alzheimer's disease\n\nAdditionally, MSCs have been used in tissue regeneration, immunological modulation, anti-inflammatory therapies, and wound healing",
            },
            {
                "role": "user",
                "content": "Does it have any side effects?",
            },
        ]
    }


chain.invoke(input_example)

# COMMAND ----------


