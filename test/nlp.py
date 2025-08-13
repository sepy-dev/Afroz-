from openai import OpenAI

client = OpenAI(
  base_url="https://ai.liara.ir/api/v1/689b4cbf527330d3c2723158",
  api_key= "" \
  )

completion = client.chat.completions.create(
  model="openai/gpt-4o-mini",
  messages=[
    {
      "role": "user",
      "content": 'سلام بگو هم افزا تقدیم میکند '
      'اولین استارت آپ سپهر'
    }
  ]
)

print(completion.choices[0].message.content)
