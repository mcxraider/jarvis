// test-integration.js
// Simple integration test for the message processor service

const { MessageProcessorService } = require('./dist/services/telegram/message-processor.service');

async function testMessageProcessor() {
  console.log('🧪 Testing MessageProcessor Integration...\n');

  try {
    // Initialize the service
    const messageProcessor = new MessageProcessorService();
    console.log('✅ MessageProcessorService initialized successfully');

    // Test text message processing (this will require OpenAI API key)
    console.log('\n📝 Testing text message processing...');

    const testMessage = 'Hello, this is a test message for poem generation!';
    const result = await messageProcessor.processTextMessage(testMessage, 12345);

    console.log('✅ Text message processed successfully');
    console.log('📜 Response:', result);
  } catch (error) {
    console.error('❌ Integration test failed:', error.message);

    // Check if it's an API key issue
    if (error.message.includes('OpenAI API key')) {
      console.log('\n💡 To complete the test, set your OpenAI API key:');
      console.log('   export OPENAI_API_KEY="your-api-key-here"');
      console.log('   npm run test:integration');
    }
  }
}

// Check if OpenAI API key is available
if (!process.env.OPENAI_API_KEY) {
  console.log('⚠️  OpenAI API key not found in environment variables');
  console.log('💡 Set OPENAI_API_KEY to test the full integration');
  console.log('   export OPENAI_API_KEY="your-api-key-here"');
  process.exit(1);
}

testMessageProcessor();
