/**
 * Poem generation utilities for AI services
 * Provides poem-specific logic, prompts, and validation
 */

/**
 * Generator for creating funny poems using AI models
 */
export class PoemGenerator {
  private readonly targetWordCount: number;

  /**
   * Creates a new PoemGenerator instance
   *
   * @param targetWordCount - Target number of words for generated poems
   */
  constructor(targetWordCount: number = 30) {
    this.targetWordCount = targetWordCount;
  }

  /**
   * Gets the system prompt for poem generation
   *
   * @returns System prompt string for AI model
   */
  getSystemPrompt(): string {
    return `You are a witty poet who creates funny, light-hearted poems. Your task is to write short, humorous poems that are exactly around ${this.targetWordCount} words long.
The poem should capture the spirit of the user's message in a humorous way.`;
  }

  /**
   * Creates a specific prompt for generating a poem about a message
   *
   * @param message - The user message to create a poem about
   * @returns Formatted prompt for the AI model
   */
  createPoemPrompt(message: string): string {
    const truncatedMessage = message.length > 200 ? message.substring(0, 200) + '...' : message;

    return `Write a funny poem about this message: "${truncatedMessage}"

Requirements:
- Exactly ${this.targetWordCount} words (give or take 2-3 words)

Please respond with only the poem, no additional text or explanations.`;
  }

  /**
   * Validates and cleans up a generated poem
   *
   * @param poem - The raw poem text from AI
   * @returns Cleaned and validated poem
   * @throws {Error} If poem doesn't meet basic requirements
   */
  validateAndCleanPoem(poem: string): string {
    // Remove any quotes or extra formatting
    let cleanedPoem = poem
      .replace(/^["']|["']$/g, '') // Remove leading/trailing quotes
      .replace(/^\s*Poem:\s*/i, '') // Remove "Poem:" prefix if present
      .replace(/^\s*Here's.*?:\s*/i, '') // Remove "Here's a poem:" type prefixes
      .trim();

    // Check if poem is too short
    if (cleanedPoem.length < 10) {
      throw new Error('Generated poem is too short');
    }

    // Check if poem is suspiciously long (might contain explanations)
    const wordCount = this.countWords(cleanedPoem);
    if (wordCount > this.targetWordCount * 2) {
      // Try to extract just the poem part
      const lines = cleanedPoem.split('\n');
      const poemLines = lines.filter(
        (line) =>
          line.trim().length > 0 &&
          !line.toLowerCase().includes('here') &&
          !line.toLowerCase().includes('this poem') &&
          !line.toLowerCase().includes('explanation'),
      );

      if (poemLines.length > 0) {
        cleanedPoem = poemLines.join('\n').trim();
      }
    }

    // Final validation
    const finalWordCount = this.countWords(cleanedPoem);
    if (finalWordCount < 5) {
      throw new Error('Generated poem is too short after cleaning');
    }

    return cleanedPoem;
  }

  /**
   * Counts words in text
   *
   * @param text - Text to count words in
   * @returns Number of words
   */
  private countWords(text: string): number {
    return text
      .trim()
      .split(/\s+/)
      .filter((word) => word.length > 0).length;
  }

  /**
   * Analyzes poem quality metrics
   *
   * @param poem - The poem to analyze
   * @returns Quality analysis object
   */
  analyzePoem(poem: string): {
    wordCount: number;
    lineCount: number;
    averageWordsPerLine: number;
    hasRhymes: boolean;
    meetsPoemStructure: boolean;
  } {
    const lines = poem.split('\n').filter((line) => line.trim().length > 0);
    const wordCount = this.countWords(poem);
    const lineCount = lines.length;
    const averageWordsPerLine = lineCount > 0 ? wordCount / lineCount : 0;

    // Basic rhyme detection (checks if line endings sound similar)
    const hasRhymes = this.detectRhymes(lines);

    // Check if it meets basic poem structure (multiple lines, reasonable word distribution)
    const meetsPoemStructure =
      lineCount >= 2 && lineCount <= 10 && averageWordsPerLine >= 2 && averageWordsPerLine <= 15;

    return {
      wordCount,
      lineCount,
      averageWordsPerLine: Math.round(averageWordsPerLine * 10) / 10,
      hasRhymes,
      meetsPoemStructure,
    };
  }

  /**
   * Basic rhyme detection for poem analysis
   *
   * @param lines - Array of poem lines
   * @returns True if potential rhymes are detected
   * @private
   */
  private detectRhymes(lines: string[]): boolean {
    if (lines.length < 2) return false;

    // Extract last word from each line
    const lastWords = lines
      .map((line) => {
        const words = line.trim().split(/\s+/);
        return words[words.length - 1]?.toLowerCase().replace(/[^\w]/g, '') || '';
      })
      .filter((word) => word.length > 0);

    if (lastWords.length < 2) return false;

    // Check for common ending patterns (basic rhyme detection)
    for (let i = 0; i < lastWords.length; i++) {
      for (let j = i + 1; j < lastWords.length; j++) {
        const word1 = lastWords[i];
        const word2 = lastWords[j];

        // Check if words end with same 2+ characters
        if (word1.length >= 2 && word2.length >= 2) {
          const ending1 = word1.slice(-2);
          const ending2 = word2.slice(-2);

          if (ending1 === ending2) {
            return true;
          }

          // Check for longer endings
          if (word1.length >= 3 && word2.length >= 3) {
            const longEnding1 = word1.slice(-3);
            const longEnding2 = word2.slice(-3);

            if (longEnding1 === longEnding2) {
              return true;
            }
          }
        }
      }
    }

    return false;
  }

  /**
   * Gets the target word count for poems
   *
   * @returns Target word count
   */
  getTargetWordCount(): number {
    return this.targetWordCount;
  }
}
