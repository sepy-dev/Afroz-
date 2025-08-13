from openai import OpenAI

client = OpenAI(
  base_url="https://ai.liara.ir/api/v1/689b4cbf527330d3c2723158",
  api_key= "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySUQiOiI2ODliNGE3YzIxMGQ1YjA5MDYxNjk4MDciLCJ0eXBlIjoiYXV0aCIsImlhdCI6MTc1NTAwOTE0Nn0.uf5plkCpHZ6nZBJMOn7ONgDJhz3F0oOslQU5GS4SUqk" \
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